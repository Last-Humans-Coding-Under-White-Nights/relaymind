# M7 comparison

Single-pass results retain provider drift and sampling uncertainty. Historical comparisons are confounded; defaults remain unchanged.

# M7-C0

## auto, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 11/12 | 4/12 | 12/12 | 12/12 | - | 7/12 | - | 0 | 0 | 0 | 1/12, 6681 ms | 45 | 9 |
| complex | 10 | 6/10 | - | 10/10 | 49/49 | 1/10 | - | - | 0 | 0 | 2 | 0/8, 0 ms | 13 | 41 |
| ambiguous | 6 | 4/6 | - | 5/6 | 6/6 | - | 4/6 | - | 0 | 0 | 1 | 0/5, 0 ms | 21 | 8 |
| mutation | 6 | 2/6 | - | 2/6 | 8/8 | - | 6/6 | 2/6 | 0 | 0 | 0 | 1/5, 6080 ms | 12 | 12 |
| adversarial | 6 | 4/6 | - | 4/6 | 7/7 | 0/1 | 3/5 | - | 0 | 0 | 2 | 1/2, 12258 ms | 11 | 16 |
| all | 40 | 27/40 | 4/12 | 33/40 | 82/82 | 1/11 | 20/29 | 2/6 | 0 | 0 | 5 | 3/32, 25019 ms | 102 | 86 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 5 |
| external | 7 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 9 / 9 / 0 | 6897 | 8237 |
| measured/local/qwen3-0.6b/final | 7 / 7 / 0 | 452 | 581 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 82 | 85 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 249 | 520 |
| measured/local/qwen3-0.6b/skill | 22 / 22 / 0 | 251 | 568 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 49 / 49 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 8 |
| local | 1 |
| external | 7 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 41 / 41 / 0 | 6299 | 8769 |
| measured/local/qwen3-0.6b/final | 1 / 1 / 0 | 455 | 455 |
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 203 | 252 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 209 | 896 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 6 / 6 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 2 |
| external | 3 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 7 / 8 / 1 | 7734 | 9922 |
| measured/local/qwen3-0.6b/final | 3 / 3 / 0 | 465 | 474 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 87 | 112 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 229 | 284 |
| measured/local/qwen3-0.6b/skill | 10 / 10 / 0 | 241 | 989 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 8 / 8 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 0 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 12 / 12 / 0 | 7301 | 8804 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 66 | 125 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 80 | 230 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 252 | 497 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 5 / 7 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 0 |
| external | 2 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 15 / 16 / 1 | 7521 | 9839 |
| measured/local/qwen3-0.6b/repair | 1 / 1 / 0 | 108 | 108 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 220 | 229 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 432 | 511 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 80 / 82 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 32 |
| local | 8 |
| external | 24 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 84 / 86 / 2 | 6984 | 9031 |
| measured/local/qwen3-0.6b/final | 11 / 11 / 0 | 455 | 581 |
| measured/local/qwen3-0.6b/repair | 9 / 9 / 0 | 85 | 125 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 219 | 284 |
| measured/local/qwen3-0.6b/skill | 42 / 42 / 0 | 401 | 896 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 59 / 102 | 1017 | 1332 |
| wasted_local_ms | 24 / 24 | 471 | 3285 |

generations per completed run: 4.46875; completed: 32; measured: 32; missing: 0; actor/purpose totals: {'local/routing': 32, 'external/external': 58, 'local/skill': 35, 'local/final': 11, 'local/repair': 7}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 128639 | 3 | None |
| output_tokens (known token subtotal) | 12329 | 3 | None |

External calls: 86 across all attempts; successful tasks: 27; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 27/40 | 0 |

## auto, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 3/12 | 12/12 | 12/12 | - | 9/12 | - | 0 | 0 | 0 | 0/12, 0 ms | 48 | 9 |
| all | 12 | 12/12 | 3/12 | 12/12 | 12/12 | - | 9/12 | - | 0 | 0 | 0 | 0/12, 0 ms | 48 | 9 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 3 |
| external | 9 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 9 / 9 / 0 | 6975 | 9500 |
| measured/local/qwen3-0.6b/final | 7 / 7 / 0 | 448 | 452 |
| measured/local/qwen3-0.6b/repair | 5 / 5 / 0 | 86 | 88 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 241 | 274 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 242 | 455 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 3 |
| external | 9 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 9 / 9 / 0 | 6975 | 9500 |
| measured/local/qwen3-0.6b/final | 7 / 7 / 0 | 448 | 452 |
| measured/local/qwen3-0.6b/repair | 5 / 5 / 0 | 86 | 88 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 241 | 274 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 242 | 455 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 24 / 48 | 343 | 1235 |
| wasted_local_ms | 9 / 9 | 2511 | 2754 |

generations per completed run: 4.75; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'local/routing': 12, 'local/skill': 24, 'local/final': 7, 'external/external': 9, 'local/repair': 5}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 11769 | 0 | 980.75 |
| output_tokens (known token subtotal) | 537 | 0 | 44.75 |

External calls: 9 across all attempts; successful tasks: 12; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 12/12 | 0 |

## external_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 0/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0/12, 0 ms | 0 | 24 |
| complex | 10 | 7/10 | - | 10/10 | 48/48 | - | - | - | 0 | 0 | 3 | 0/7, 0 ms | 0 | 39 |
| ambiguous | 6 | 0/6 | - | 0/6 | 11/11 | - | - | - | 0 | 0 | 0 | 0/6, 0 ms | 0 | 13 |
| mutation | 6 | 1/6 | - | 1/6 | 7/7 | - | - | 1/6 | 0 | 0 | 0 | 0/6, 0 ms | 0 | 13 |
| adversarial | 6 | 5/6 | - | 6/6 | 4/4 | - | - | - | 0 | 0 | 0 | 0/6, 0 ms | 0 | 10 |
| all | 40 | 25/40 | 0/12 | 29/40 | 82/82 | - | - | 1/6 | 0 | 0 | 3 | 0/37, 0 ms | 0 | 99 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 24 / 24 / 0 | 6236 | 7594 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 12 | 0 | 0 | None | None | None |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 48 / 48 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 7 |
| local | 0 |
| external | 7 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 39 / 39 / 0 | 7142 | 9386 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 10 | 0 | 0 | None | None | None |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 7 / 11 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 0 |
| external | 6 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 13 / 13 / 0 | 7400 | 8393 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 6 | 0 | 0 | None | None | None |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 7 / 7 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 0 |
| external | 6 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 13 / 13 / 0 | 7071 | 9016 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 6 | 0 | 0 | None | None | None |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 0 |
| external | 6 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 10 / 10 / 0 | 7034 | 9261 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 6 | 0 | 0 | None | None | None |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 78 / 82 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 37 |
| local | 0 |
| external | 37 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 99 / 99 / 0 | 6996 | 9016 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 40 | 0 | 0 | None | None | None |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 0 / 0 | - | - |
| wasted_local_ms | 37 / 37 | 0 | 0 |

generations per completed run: 2.2972972972972974; completed: 37; measured: 37; missing: 0; actor/purpose totals: {'external/external': 85}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 151308 | 2 | None |
| output_tokens (known token subtotal) | 12682 | 2 | None |

External calls: 99 across all attempts; successful tasks: 25; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 25/40 | 0 |

## external_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 0/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0/12, 0 ms | 0 | 24 |
| all | 12 | 12/12 | 0/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0/12, 0 ms | 0 | 24 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 24 / 24 / 0 | 6675 | 8658 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 12 | 0 | 0 | None | None | None |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 24 / 24 / 0 | 6675 | 8658 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 12 | 0 | 0 | None | None | None |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 0 / 0 | - | - |
| wasted_local_ms | 12 / 12 | 0 | 0 |

generations per completed run: 2.0; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'external/external': 24}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 34968 | 0 | 2914.0 |
| output_tokens (known token subtotal) | 1861 | 0 | 155.08333333333334 |

External calls: 24 across all attempts; successful tasks: 12; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 12/12 | 0 |

## local_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 4/12 | 4/12 | 11/12 | 11/11 | - | - | - | 0 | 0 | 7 | 0/5, 0 ms | 45 | 0 |
| complex | 10 | 0/10 | - | 1/10 | 1/1 | - | - | - | 0 | 0 | 9 | 0/1, 0 ms | 13 | 0 |
| ambiguous | 6 | 2/6 | - | 4/6 | 5/5 | - | - | - | 0 | 0 | 4 | 0/2, 0 ms | 21 | 0 |
| mutation | 6 | 0/6 | - | 0/6 | 2/2 | - | - | 0/6 | 0 | 0 | 6 | 0/0, 0 ms | 12 | 0 |
| adversarial | 6 | 4/6 | - | 4/6 | 3/3 | - | - | - | 0 | 0 | 4 | 0/0, 0 ms | 11 | 0 |
| all | 40 | 10/40 | 4/12 | 20/40 | 22/22 | - | - | 0/6 | 0 | 0 | 30 | 0/8, 0 ms | 102 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 11 / 11 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 5 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 8 / 8 / 0 | 221 | 584 |
| measured/local/qwen3-0.6b/repair | 3 / 3 / 0 | 97 | 107 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 237 | 325 |
| measured/local/qwen3-0.6b/skill | 22 / 22 / 0 | 233 | 432 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 1 / 1 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 1 |
| local | 1 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 1 / 1 / 0 | 245 | 245 |
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 201 | 247 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 220 | 293 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 5 / 5 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 2 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 3 / 3 / 0 | 232 | 258 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 89 | 113 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 237 | 282 |
| measured/local/qwen3-0.6b/skill | 10 / 10 / 0 | 236 | 480 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 2 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 0 |
| local | 0 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 69 | 116 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 68 | 233 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 239 | 244 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 3 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 0 |
| local | 0 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 1 / 1 / 0 | 107 | 107 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 217 | 241 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 196 | 419 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 21 / 22 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 8 |
| local | 8 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 12 / 12 / 0 | 231 | 584 |
| measured/local/qwen3-0.6b/repair | 8 / 8 / 0 | 97 | 116 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 224 | 273 |
| measured/local/qwen3-0.6b/skill | 42 / 42 / 0 | 235 | 432 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 59 / 102 | 1037 | 1272 |
| wasted_local_ms | 30 / 30 | 510 | 2721 |

generations per completed run: 4.0; completed: 8; measured: 8; missing: 0; actor/purpose totals: {'local/routing': 8, 'local/skill': 16, 'local/final': 8}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 10; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 10/40 | 0 |

## local_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 3/12 | 3/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 9 | 0/3, 0 ms | 48 | 0 |
| all | 12 | 3/12 | 3/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 9 | 0/3, 0 ms | 48 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 3 |
| local | 3 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 7 / 7 / 0 | 218 | 244 |
| measured/local/qwen3-0.6b/repair | 5 / 5 / 0 | 86 | 90 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 235 | 262 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 234 | 447 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 3 |
| local | 3 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 7 / 7 / 0 | 218 | 244 |
| measured/local/qwen3-0.6b/repair | 5 / 5 / 0 | 86 | 90 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 235 | 262 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 234 | 447 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 24 / 48 | 202 | 1137 |
| wasted_local_ms | 9 / 9 | 2414 | 2810 |

generations per completed run: 4.0; completed: 3; measured: 3; missing: 0; actor/purpose totals: {'local/routing': 3, 'local/skill': 6, 'local/final': 3}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 3; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 3/12 | 0 |

## Section 12.3 targets (English simple subset)

- auto: local_completion 4/12 (target 80%, not met); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- external_only: local_completion 0/12 (target 80%, not met); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- local_only: local_completion 4/12 (target 80%, not met); tool_correctness 11/12 (target 95%, not met); missed confirmations 0 (met)

# M7-C1

## auto, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 11/12 | 6/12 | 12/12 | 12/12 | - | 5/12 | - | 0 | 0 | 0 | 0/12, 0 ms | 38 | 6 |
| complex | 10 | 3/10 | - | 10/10 | 51/51 | 1/10 | - | - | 0 | 0 | 6 | 0/4, 0 ms | 12 | 41 |
| ambiguous | 6 | 4/6 | - | 5/6 | 6/6 | - | 3/6 | - | 0 | 0 | 1 | 0/5, 0 ms | 18 | 7 |
| mutation | 6 | 2/6 | - | 2/6 | 8/8 | - | 6/6 | 2/6 | 0 | 0 | 0 | 1/5, 9004 ms | 12 | 12 |
| adversarial | 6 | 4/6 | - | 4/6 | 6/6 | 0/1 | 3/5 | - | 0 | 0 | 2 | 1/2, 13136 ms | 11 | 15 |
| all | 40 | 24/40 | 6/12 | 33/40 | 83/83 | 1/11 | 17/29 | 2/6 | 0 | 0 | 9 | 2/28, 22140 ms | 91 | 81 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 7 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 6 / 6 / 0 | 7317 | 9178 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 80 | 82 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 235 | 261 |
| measured/local/qwen3-0.6b/skill | 22 / 22 / 0 | 227 | 579 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 51 / 51 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 4 |
| local | 1 |
| external | 3 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 40 / 41 / 1 | 8366 | 10825 |
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 198 | 244 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 198 | 450 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 6 / 6 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 3 |
| external | 2 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 6 / 7 / 1 | 8973 | 11021 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 87 | 111 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 245 | 295 |
| measured/local/qwen3-0.6b/skill | 10 / 10 / 0 | 241 | 735 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 8 / 8 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 0 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 12 / 12 / 0 | 8409 | 15359 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 66 | 109 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 79 | 226 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 231 | 463 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 6 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 0 |
| external | 2 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 15 / 15 / 0 | 8634 | 11523 |
| measured/local/qwen3-0.6b/repair | 1 / 1 / 0 | 108 | 108 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 205 | 237 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 410 | 481 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 81 / 83 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 28 |
| local | 11 |
| external | 17 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 79 / 81 / 2 | 8480 | 11119 |
| measured/local/qwen3-0.6b/repair | 9 / 9 / 0 | 82 | 111 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 213 | 279 |
| measured/local/qwen3-0.6b/skill | 42 / 42 / 0 | 366 | 579 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 59 / 91 | 1006 | 1227 |
| wasted_local_ms | 17 / 17 | 527 | 2881 |

generations per completed run: 3.6785714285714284; completed: 28; measured: 28; missing: 0; actor/purpose totals: {'local/routing': 28, 'external/external': 33, 'local/skill': 35, 'local/repair': 7}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 107965 | 9 | None |
| output_tokens (known token subtotal) | 7845 | 9 | None |

External calls: 81 across all attempts; successful tasks: 24; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 24/40 | 0 |

## auto, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 3/12 | 12/12 | 12/12 | - | 9/12 | - | 0 | 0 | 0 | 1/12, 8611 ms | 47 | 10 |
| all | 12 | 12/12 | 3/12 | 12/12 | 12/12 | - | 9/12 | - | 0 | 0 | 0 | 1/12, 8611 ms | 47 | 10 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 3 |
| external | 9 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 10 / 10 / 0 | 8370 | 11319 |
| measured/local/qwen3-0.6b/final | 5 / 5 / 0 | 459 | 468 |
| measured/local/qwen3-0.6b/repair | 6 / 6 / 0 | 85 | 94 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 251 | 266 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 276 | 453 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 3 |
| external | 9 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 10 / 10 / 0 | 8370 | 11319 |
| measured/local/qwen3-0.6b/final | 5 / 5 / 0 | 459 | 468 |
| measured/local/qwen3-0.6b/repair | 6 / 6 / 0 | 85 | 94 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 251 | 266 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 276 | 453 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 24 / 47 | 197 | 1094 |
| wasted_local_ms | 9 / 9 | 2616 | 2726 |

generations per completed run: 4.75; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'local/routing': 12, 'local/skill': 24, 'local/final': 5, 'external/external': 10, 'local/repair': 6}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 13491 | 0 | 1124.25 |
| output_tokens (known token subtotal) | 644 | 0 | 53.666666666666664 |

External calls: 10 across all attempts; successful tasks: 12; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 12/12 | 0 |

## local_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 6/12 | 6/12 | 11/12 | 11/11 | - | - | - | 0 | 0 | 5 | 0/7, 0 ms | 38 | 0 |
| complex | 10 | 0/10 | - | 1/10 | 1/1 | - | - | - | 0 | 0 | 9 | 0/1, 0 ms | 12 | 0 |
| ambiguous | 6 | 2/6 | - | 4/6 | 5/5 | - | - | - | 0 | 0 | 3 | 0/3, 0 ms | 18 | 0 |
| mutation | 6 | 0/6 | - | 0/6 | 2/2 | - | - | 0/6 | 0 | 0 | 6 | 0/0, 0 ms | 12 | 0 |
| adversarial | 6 | 4/6 | - | 4/6 | 3/3 | - | - | - | 0 | 0 | 4 | 0/0, 0 ms | 11 | 0 |
| all | 40 | 12/40 | 6/12 | 20/40 | 22/22 | - | - | 0/6 | 0 | 0 | 27 | 0/11, 0 ms | 91 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 11 / 11 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 7 |
| local | 7 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 81 | 87 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 236 | 274 |
| measured/local/qwen3-0.6b/skill | 22 / 22 / 0 | 199 | 383 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 1 / 1 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 1 |
| local | 1 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 202 | 244 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 205 | 211 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 5 / 5 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 3 |
| local | 3 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 95 | 138 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 238 | 269 |
| measured/local/qwen3-0.6b/skill | 10 / 10 / 0 | 219 | 431 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 2 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 0 |
| local | 0 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 69 | 118 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 80 | 233 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 210 | 234 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 3 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 0 |
| local | 0 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 1 / 1 / 0 | 107 | 107 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 206 | 236 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 166 | 400 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 21 / 22 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 11 |
| local | 11 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 9 / 9 / 0 | 87 | 138 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 220 | 269 |
| measured/local/qwen3-0.6b/skill | 42 / 42 / 0 | 211 | 400 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 59 / 91 | 1026 | 1539 |
| wasted_local_ms | 27 / 27 | 471 | 3171 |

generations per completed run: 3.0; completed: 11; measured: 11; missing: 0; actor/purpose totals: {'local/routing': 11, 'local/skill': 22}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 12; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 12/40 | 0 |

## local_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 4/12 | 4/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 8 | 0/4, 0 ms | 46 | 0 |
| all | 12 | 4/12 | 4/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 8 | 0/4, 0 ms | 46 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 4 |
| local | 4 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 5 / 5 / 0 | 208 | 219 |
| measured/local/qwen3-0.6b/repair | 5 / 5 / 0 | 87 | 88 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 238 | 272 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 217 | 446 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 4 |
| local | 4 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 5 / 5 / 0 | 208 | 219 |
| measured/local/qwen3-0.6b/repair | 5 / 5 / 0 | 87 | 88 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 238 | 272 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 217 | 446 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 24 / 46 | 185 | 1132 |
| wasted_local_ms | 8 / 8 | 2269 | 2496 |

generations per completed run: 3.5; completed: 4; measured: 4; missing: 0; actor/purpose totals: {'local/routing': 4, 'local/skill': 8, 'local/final': 2}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 4; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 4/12 | 0 |

## Section 12.3 targets (English simple subset)

- auto: local_completion 6/12 (target 80%, not met); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- local_only: local_completion 6/12 (target 80%, not met); tool_correctness 11/12 (target 95%, not met); missed confirmations 0 (met)

# M7-C2

## auto, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 11/12 | 4/12 | 12/12 | 12/12 | - | 7/12 | - | 0 | 0 | 0 | 2/12, 18757 ms | 45 | 10 |
| complex | 10 | 5/10 | - | 10/10 | 28/28 | 1/10 | - | - | 0 | 0 | 4 | 0/6, 0 ms | 13 | 18 |
| ambiguous | 6 | 5/6 | - | 5/6 | 6/6 | - | 4/6 | - | 0 | 0 | 1 | 0/5, 0 ms | 21 | 8 |
| mutation | 6 | 2/6 | - | 2/6 | 8/8 | - | 6/6 | 2/6 | 0 | 0 | 0 | 1/5, 8379 ms | 12 | 12 |
| adversarial | 6 | 4/6 | - | 4/6 | 8/8 | 0/1 | 3/5 | - | 0 | 0 | 2 | 1/2, 14227 ms | 11 | 16 |
| all | 40 | 27/40 | 4/12 | 33/40 | 62/62 | 1/11 | 20/29 | 2/6 | 0 | 0 | 7 | 4/30, 41363 ms | 102 | 64 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 5 |
| external | 7 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 10 / 10 / 0 | 8528 | 10379 |
| measured/local/qwen3-0.6b/final | 8 / 8 / 0 | 447 | 510 |
| measured/local/qwen3-0.6b/repair | 3 / 3 / 0 | 82 | 83 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 244 | 278 |
| measured/local/qwen3-0.6b/skill | 22 / 22 / 0 | 225 | 576 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 28 / 28 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 1 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 18 / 18 / 0 | 9236 | 13332 |
| measured/local/qwen3-0.6b/final | 1 / 1 / 0 | 441 | 441 |
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 202 | 246 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 207 | 478 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 6 / 6 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 2 |
| external | 3 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 8 / 8 / 0 | 8253 | 10746 |
| measured/local/qwen3-0.6b/final | 3 / 3 / 0 | 454 | 487 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 87 | 110 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 232 | 275 |
| measured/local/qwen3-0.6b/skill | 10 / 10 / 0 | 237 | 678 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 8 / 8 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 0 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 12 / 12 / 0 | 7737 | 11047 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 84 | 124 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 76 | 225 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 248 | 486 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 6 / 8 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 0 |
| external | 2 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 15 / 16 / 1 | 8697 | 9907 |
| measured/local/qwen3-0.6b/repair | 1 / 1 / 0 | 109 | 109 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 217 | 235 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 396 | 487 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 60 / 62 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 30 |
| local | 8 |
| external | 22 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 63 / 64 / 1 | 8603 | 10981 |
| measured/local/qwen3-0.6b/final | 12 / 12 / 0 | 447 | 510 |
| measured/local/qwen3-0.6b/repair | 8 / 8 / 0 | 84 | 124 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 217 | 265 |
| measured/local/qwen3-0.6b/skill | 42 / 42 / 0 | 392 | 576 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 59 / 102 | 1001 | 1147 |
| wasted_local_ms | 22 / 22 | 518 | 2772 |

generations per completed run: 4.066666666666666; completed: 30; measured: 30; missing: 0; actor/purpose totals: {'local/routing': 30, 'external/external': 39, 'local/skill': 35, 'local/final': 12, 'local/repair': 6}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 82797 | 7 | None |
| output_tokens (known token subtotal) | 8670 | 7 | None |

External calls: 64 across all attempts; successful tasks: 27; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 27/40 | 0 |

## auto, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 11/12 | 2/12 | 12/12 | 12/12 | - | 9/12 | - | 0 | 0 | 0 | 2/12, 19823 ms | 48 | 11 |
| all | 12 | 11/12 | 2/12 | 12/12 | 12/12 | - | 9/12 | - | 0 | 0 | 0 | 2/12, 19823 ms | 48 | 11 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 3 |
| external | 9 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 11 / 11 / 0 | 9796 | 13352 |
| measured/local/qwen3-0.6b/final | 6 / 6 / 0 | 444 | 447 |
| measured/local/qwen3-0.6b/repair | 6 / 6 / 0 | 87 | 89 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 236 | 271 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 237 | 472 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 3 |
| external | 9 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 11 / 11 / 0 | 9796 | 13352 |
| measured/local/qwen3-0.6b/final | 6 / 6 / 0 | 444 | 447 |
| measured/local/qwen3-0.6b/repair | 6 / 6 / 0 | 87 | 89 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 236 | 271 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 237 | 472 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 24 / 48 | 313 | 1239 |
| wasted_local_ms | 9 / 9 | 2556 | 2762 |

generations per completed run: 4.916666666666667; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'local/routing': 12, 'local/skill': 24, 'local/final': 6, 'external/external': 11, 'local/repair': 6}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 15039 | 0 | 1367.1818181818182 |
| output_tokens (known token subtotal) | 822 | 0 | 74.72727272727273 |

External calls: 11 across all attempts; successful tasks: 11; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 11/12 | 0 |

## local_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 4/12 | 4/12 | 11/12 | 11/11 | - | - | - | 0 | 0 | 7 | 0/5, 0 ms | 45 | 0 |
| complex | 10 | 0/10 | - | 1/10 | 1/1 | - | - | - | 0 | 0 | 9 | 0/1, 0 ms | 13 | 0 |
| ambiguous | 6 | 1/6 | - | 4/6 | 5/5 | - | - | - | 0 | 0 | 4 | 0/2, 0 ms | 21 | 0 |
| mutation | 6 | 0/6 | - | 0/6 | 2/2 | - | - | 0/6 | 0 | 0 | 6 | 0/0, 0 ms | 12 | 0 |
| adversarial | 6 | 4/6 | - | 4/6 | 3/3 | - | - | - | 0 | 0 | 4 | 0/0, 0 ms | 11 | 0 |
| all | 40 | 9/40 | 4/12 | 20/40 | 22/22 | - | - | 0/6 | 0 | 0 | 30 | 0/8, 0 ms | 102 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 11 / 11 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 5 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 8 / 8 / 0 | 208 | 241 |
| measured/local/qwen3-0.6b/repair | 3 / 3 / 0 | 86 | 87 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 251 | 298 |
| measured/local/qwen3-0.6b/skill | 22 / 22 / 0 | 229 | 436 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 1 / 1 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 1 |
| local | 1 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 1 / 1 / 0 | 199 | 199 |
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 204 | 251 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 204 | 220 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 5 / 5 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 2 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 3 / 3 / 0 | 229 | 245 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 71 | 139 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 229 | 288 |
| measured/local/qwen3-0.6b/skill | 10 / 10 / 0 | 229 | 498 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 2 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 0 |
| local | 0 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 69 | 118 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 82 | 247 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 231 | 245 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 3 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 0 |
| local | 0 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 1 / 1 / 0 | 106 | 106 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 202 | 235 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 161 | 401 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 21 / 22 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 8 |
| local | 8 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 12 / 12 / 0 | 208 | 245 |
| measured/local/qwen3-0.6b/repair | 8 / 8 / 0 | 86 | 139 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 220 | 271 |
| measured/local/qwen3-0.6b/skill | 42 / 42 / 0 | 229 | 436 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 59 / 102 | 1025 | 1254 |
| wasted_local_ms | 30 / 30 | 527 | 2749 |

generations per completed run: 4.0; completed: 8; measured: 8; missing: 0; actor/purpose totals: {'local/routing': 8, 'local/skill': 16, 'local/final': 8}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 9; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 9/40 | 0 |

## local_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 3/12 | 3/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 9 | 0/3, 0 ms | 48 | 0 |
| all | 12 | 3/12 | 3/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 9 | 0/3, 0 ms | 48 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 3 |
| local | 3 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 7 / 7 / 0 | 213 | 465 |
| measured/local/qwen3-0.6b/repair | 5 / 5 / 0 | 85 | 93 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 226 | 283 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 225 | 445 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 3 |
| local | 3 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 7 / 7 / 0 | 213 | 465 |
| measured/local/qwen3-0.6b/repair | 5 / 5 / 0 | 85 | 93 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 226 | 283 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 225 | 445 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 24 / 48 | 192 | 1181 |
| wasted_local_ms | 9 / 9 | 2408 | 3050 |

generations per completed run: 4.0; completed: 3; measured: 3; missing: 0; actor/purpose totals: {'local/routing': 3, 'local/skill': 6, 'local/final': 3}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 3; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 3/12 | 0 |

## Section 12.3 targets (English simple subset)

- auto: local_completion 4/12 (target 80%, not met); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- local_only: local_completion 4/12 (target 80%, not met); tool_correctness 11/12 (target 95%, not met); missed confirmations 0 (met)

# M7-C3

## auto, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 11/12 | 4/12 | 12/12 | 5/5 | - | 7/12 | - | 0 | 0 | 0 | 1/12, 6855 ms | 38 | 9 |
| complex | 10 | 5/10 | - | 10/10 | 25/25 | 1/10 | - | - | 0 | 0 | 4 | 0/6, 0 ms | 12 | 26 |
| ambiguous | 6 | 4/6 | - | 5/6 | 3/3 | - | 4/6 | - | 0 | 0 | 1 | 0/5, 0 ms | 18 | 8 |
| mutation | 6 | 2/6 | - | 2/6 | 8/8 | - | 6/6 | 2/6 | 0 | 0 | 0 | 1/5, 9389 ms | 12 | 12 |
| adversarial | 6 | 4/6 | - | 4/6 | 5/5 | 1/1 | 3/5 | - | 0 | 0 | 2 | 0/2, 0 ms | 9 | 13 |
| all | 40 | 26/40 | 4/12 | 33/40 | 46/46 | 2/11 | 20/29 | 2/6 | 0 | 0 | 7 | 2/30, 16244 ms | 89 | 68 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 5 / 5 |
| prefetch accuracy | 7 / 7 |

Missing proposal traces: 0; gateway proposals: 7; prefetch diagnostics: {'admitted': 7, 'rejected': 0, 'executed': 7}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 5 |
| external | 7 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 9 / 9 / 0 | 8361 | 12952 |
| measured/local/qwen3-0.6b/final | 6 / 6 / 0 | 38 | 495 |
| measured/local/qwen3-0.6b/repair | 5 / 5 / 0 | 79 | 82 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 241 | 387 |
| measured/local/qwen3-0.6b/skill | 15 / 15 / 0 | 448 | 646 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 25 / 25 |
| prefetch accuracy | 13 / 13 |

Missing proposal traces: 0; gateway proposals: 13; prefetch diagnostics: {'admitted': 13, 'rejected': 0, 'executed': 13}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 1 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 26 / 26 / 0 | 8084 | 10030 |
| measured/local/qwen3-0.6b/final | 1 / 1 / 0 | 41 | 41 |
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 206 | 256 |
| measured/local/qwen3-0.6b/skill | 1 / 1 / 0 | 446 | 446 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 3 / 3 |
| prefetch accuracy | 3 / 3 |

Missing proposal traces: 0; gateway proposals: 3; prefetch diagnostics: {'admitted': 3, 'rejected': 0, 'executed': 3}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 2 |
| external | 3 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 7 / 8 / 1 | 8735 | 9279 |
| measured/local/qwen3-0.6b/final | 3 / 3 / 0 | 38 | 39 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 88 | 111 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 226 | 361 |
| measured/local/qwen3-0.6b/skill | 7 / 7 / 0 | 481 | 714 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 8 / 8 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 0 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 12 / 12 / 0 | 8773 | 10998 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 66 | 115 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 77 | 215 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 228 | 492 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 5 |
| prefetch accuracy | 2 / 3 |

Missing proposal traces: 0; gateway proposals: 3; prefetch diagnostics: {'admitted': 3, 'rejected': 0, 'executed': 5}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 1 |
| external | 1 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 13 / 13 / 0 | 8995 | 9968 |
| measured/local/qwen3-0.6b/final | 1 / 1 / 0 | 39 | 39 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 200 | 234 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 424 | 471 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 45 / 46 |
| prefetch accuracy | 25 / 26 |

Missing proposal traces: 0; gateway proposals: 26; prefetch diagnostics: {'admitted': 26, 'rejected': 0, 'executed': 28}.

| answer owner | completed runs |
|---|---|
| eligible | 30 |
| local | 9 |
| external | 21 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 67 / 68 / 1 | 8500 | 10838 |
| measured/local/qwen3-0.6b/final | 11 / 11 / 0 | 39 | 495 |
| measured/local/qwen3-0.6b/repair | 9 / 9 / 0 | 80 | 115 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 213 | 325 |
| measured/local/qwen3-0.6b/skill | 29 / 29 / 0 | 448 | 646 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 48 / 89 | 1013 | 1200 |
| wasted_local_ms | 21 / 21 | 515 | 2930 |

generations per completed run: 3.566666666666667; completed: 30; measured: 30; missing: 0; actor/purpose totals: {'local/routing': 30, 'external/external': 35, 'local/skill': 24, 'local/final': 11, 'local/repair': 7}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 100385 | 5 | None |
| output_tokens (known token subtotal) | 8356 | 5 | None |

External calls: 68 across all attempts; successful tasks: 26; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 26/40 | 0 |

## auto, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 3/12 | 12/12 | 4/4 | - | 9/12 | - | 0 | 0 | 0 | 0/12, 0 ms | 40 | 9 |
| all | 12 | 12/12 | 3/12 | 12/12 | 4/4 | - | 9/12 | - | 0 | 0 | 0 | 0/12, 0 ms | 40 | 9 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 8 / 8 |

Missing proposal traces: 0; gateway proposals: 8; prefetch diagnostics: {'admitted': 8, 'rejected': 0, 'executed': 8}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 3 |
| external | 9 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 9 / 9 / 0 | 8602 | 10375 |
| measured/local/qwen3-0.6b/final | 8 / 8 / 0 | 38 | 41 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 87 | 94 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 236 | 259 |
| measured/local/qwen3-0.6b/skill | 16 / 16 / 0 | 455 | 878 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 8 / 8 |

Missing proposal traces: 0; gateway proposals: 8; prefetch diagnostics: {'admitted': 8, 'rejected': 0, 'executed': 8}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 3 |
| external | 9 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 9 / 9 / 0 | 8602 | 10375 |
| measured/local/qwen3-0.6b/final | 8 / 8 / 0 | 38 | 41 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 87 | 94 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 236 | 259 |
| measured/local/qwen3-0.6b/skill | 16 / 16 / 0 | 455 | 878 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 16 / 40 | 994 | 1125 |
| wasted_local_ms | 9 / 9 | 2083 | 2868 |

generations per completed run: 4.083333333333333; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'local/routing': 12, 'local/skill': 16, 'local/final': 8, 'external/external': 9, 'local/repair': 4}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 11717 | 0 | 976.4166666666666 |
| output_tokens (known token subtotal) | 549 | 0 | 45.75 |

External calls: 9 across all attempts; successful tasks: 12; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 12/12 | 0 |

## local_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 4/12 | 4/12 | 11/12 | 4/4 | - | - | - | 0 | 0 | 7 | 0/5, 0 ms | 38 | 0 |
| complex | 10 | 0/10 | - | 1/10 | 0/0 | - | - | - | 0 | 0 | 9 | 0/1, 0 ms | 12 | 0 |
| ambiguous | 6 | 1/6 | - | 4/6 | 2/2 | - | - | - | 0 | 0 | 4 | 0/2, 0 ms | 18 | 0 |
| mutation | 6 | 0/6 | - | 0/6 | 2/2 | - | - | 0/6 | 0 | 0 | 6 | 0/0, 0 ms | 12 | 0 |
| adversarial | 6 | 4/6 | - | 4/6 | 1/1 | - | - | - | 0 | 0 | 3 | 0/1, 0 ms | 9 | 0 |
| all | 40 | 9/40 | 4/12 | 20/40 | 9/9 | - | - | 0/6 | 0 | 0 | 29 | 0/9, 0 ms | 89 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 7 / 7 |

Missing proposal traces: 0; gateway proposals: 7; prefetch diagnostics: {'admitted': 7, 'rejected': 0, 'executed': 7}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 5 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 6 / 6 / 0 | 38 | 238 |
| measured/local/qwen3-0.6b/repair | 5 / 5 / 0 | 77 | 86 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 239 | 273 |
| measured/local/qwen3-0.6b/skill | 15 / 15 / 0 | 193 | 242 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 0 / 0 |
| prefetch accuracy | 1 / 1 |

Missing proposal traces: 0; gateway proposals: 1; prefetch diagnostics: {'admitted': 1, 'rejected': 0, 'executed': 1}.

| answer owner | completed runs |
|---|---|
| eligible | 1 |
| local | 1 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 1 / 1 / 0 | 38 | 38 |
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 207 | 258 |
| measured/local/qwen3-0.6b/skill | 1 / 1 / 0 | 177 | 177 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 2 |
| prefetch accuracy | 3 / 3 |

Missing proposal traces: 0; gateway proposals: 3; prefetch diagnostics: {'admitted': 3, 'rejected': 0, 'executed': 3}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 2 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 3 / 3 / 0 | 38 | 41 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 86 | 143 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 242 | 269 |
| measured/local/qwen3-0.6b/skill | 7 / 7 / 0 | 217 | 237 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 2 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 0 |
| local | 0 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 69 | 119 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 81 | 224 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 204 | 238 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 0 / 1 |
| prefetch accuracy | 2 / 3 |

Missing proposal traces: 0; gateway proposals: 3; prefetch diagnostics: {'admitted': 3, 'rejected': 0, 'executed': 5}.

| answer owner | completed runs |
|---|---|
| eligible | 1 |
| local | 1 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 1 / 1 / 0 | 39 | 39 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 205 | 239 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 164 | 168 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 8 / 9 |
| prefetch accuracy | 13 / 14 |

Missing proposal traces: 0; gateway proposals: 14; prefetch diagnostics: {'admitted': 14, 'rejected': 0, 'executed': 16}.

| answer owner | completed runs |
|---|---|
| eligible | 9 |
| local | 9 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 11 / 11 / 0 | 38 | 238 |
| measured/local/qwen3-0.6b/repair | 9 / 9 / 0 | 84 | 143 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 224 | 269 |
| measured/local/qwen3-0.6b/skill | 29 / 29 / 0 | 202 | 238 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 48 / 89 | 1046 | 1353 |
| wasted_local_ms | 29 / 29 | 458 | 2739 |

generations per completed run: 3.0; completed: 9; measured: 9; missing: 0; actor/purpose totals: {'local/routing': 9, 'local/skill': 9, 'local/final': 9}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 9; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 9/40 | 0 |

## local_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 3/12 | 3/12 | 12/12 | 4/4 | - | - | - | 0 | 0 | 9 | 0/3, 0 ms | 40 | 0 |
| all | 12 | 3/12 | 3/12 | 12/12 | 4/4 | - | - | - | 0 | 0 | 9 | 0/3, 0 ms | 40 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 8 / 8 |

Missing proposal traces: 0; gateway proposals: 8; prefetch diagnostics: {'admitted': 8, 'rejected': 0, 'executed': 8}.

| answer owner | completed runs |
|---|---|
| eligible | 3 |
| local | 3 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 8 / 8 / 0 | 38 | 40 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 84 | 89 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 245 | 269 |
| measured/local/qwen3-0.6b/skill | 16 / 16 / 0 | 183 | 240 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 8 / 8 |

Missing proposal traces: 0; gateway proposals: 8; prefetch diagnostics: {'admitted': 8, 'rejected': 0, 'executed': 8}.

| answer owner | completed runs |
|---|---|
| eligible | 3 |
| local | 3 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 8 / 8 / 0 | 38 | 40 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 84 | 89 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 245 | 269 |
| measured/local/qwen3-0.6b/skill | 16 / 16 / 0 | 183 | 240 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 16 / 40 | 1008 | 1557 |
| wasted_local_ms | 9 / 9 | 1766 | 2518 |

generations per completed run: 3.0; completed: 3; measured: 3; missing: 0; actor/purpose totals: {'local/routing': 3, 'local/skill': 3, 'local/final': 3}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 3; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 3/12 | 0 |

## Section 12.3 targets (English simple subset)

- auto: local_completion 4/12 (target 80%, not met); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- local_only: local_completion 4/12 (target 80%, not met); tool_correctness 11/12 (target 95%, not met); missed confirmations 0 (met)

# M7-C4

## auto, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 11/12 | 6/12 | 12/12 | 5/5 | - | 5/12 | - | 0 | 0 | 0 | 0/12, 0 ms | 31 | 6 |
| complex | 10 | 4/10 | - | 10/10 | 14/14 | 1/10 | - | - | 0 | 0 | 5 | 0/5, 0 ms | 11 | 17 |
| ambiguous | 6 | 5/6 | - | 5/6 | 3/3 | - | 3/6 | - | 0 | 0 | 1 | 0/5, 0 ms | 15 | 7 |
| mutation | 6 | 2/6 | - | 2/6 | 8/8 | - | 6/6 | 2/6 | 0 | 0 | 0 | 1/5, 7280 ms | 12 | 12 |
| adversarial | 6 | 4/6 | - | 4/6 | 5/5 | 1/1 | 3/5 | - | 0 | 0 | 2 | 0/2, 0 ms | 8 | 13 |
| all | 40 | 26/40 | 6/12 | 33/40 | 35/35 | 2/11 | 17/29 | 2/6 | 0 | 0 | 8 | 1/29, 7280 ms | 77 | 55 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 5 / 5 |
| prefetch accuracy | 7 / 7 |

Missing proposal traces: 0; gateway proposals: 7; prefetch diagnostics: {'admitted': 7, 'rejected': 0, 'executed': 7}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 7 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 6 / 6 / 0 | 8876 | 11598 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 62 | 82 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 234 | 316 |
| measured/local/qwen3-0.6b/skill | 15 / 15 / 0 | 454 | 643 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 14 / 14 |
| prefetch accuracy | 13 / 13 |

Missing proposal traces: 0; gateway proposals: 13; prefetch diagnostics: {'admitted': 13, 'rejected': 0, 'executed': 13}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 1 |
| external | 4 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 17 / 17 / 0 | 8115 | 11855 |
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 201 | 247 |
| measured/local/qwen3-0.6b/skill | 1 / 1 / 0 | 428 | 428 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 3 / 3 |
| prefetch accuracy | 3 / 3 |

Missing proposal traces: 0; gateway proposals: 3; prefetch diagnostics: {'admitted': 3, 'rejected': 0, 'executed': 3}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 3 |
| external | 2 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 7 / 7 / 0 | 8583 | 14020 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 87 | 135 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 226 | 278 |
| measured/local/qwen3-0.6b/skill | 7 / 7 / 0 | 503 | 683 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 8 / 8 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 0 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 12 / 12 / 0 | 8833 | 10862 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 66 | 116 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 82 | 225 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 230 | 463 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 5 |
| prefetch accuracy | 2 / 3 |

Missing proposal traces: 0; gateway proposals: 3; prefetch diagnostics: {'admitted': 3, 'rejected': 0, 'executed': 5}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 1 |
| external | 1 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 13 / 13 / 0 | 9150 | 12011 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 210 | 237 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 449 | 531 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 34 / 35 |
| prefetch accuracy | 25 / 26 |

Missing proposal traces: 0; gateway proposals: 26; prefetch diagnostics: {'admitted': 26, 'rejected': 0, 'executed': 28}.

| answer owner | completed runs |
|---|---|
| eligible | 29 |
| local | 12 |
| external | 17 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 55 / 55 / 0 | 8652 | 11855 |
| measured/local/qwen3-0.6b/repair | 8 / 8 / 0 | 76 | 135 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 219 | 264 |
| measured/local/qwen3-0.6b/skill | 29 / 29 / 0 | 454 | 643 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 48 / 77 | 1016 | 1133 |
| wasted_local_ms | 17 / 17 | 529 | 2647 |

generations per completed run: 3.0344827586206895; completed: 29; measured: 29; missing: 0; actor/purpose totals: {'local/routing': 29, 'external/external': 29, 'local/skill': 24, 'local/repair': 6}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 70756 | 8 | None |
| output_tokens (known token subtotal) | 6290 | 8 | None |

External calls: 55 across all attempts; successful tasks: 26; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 26/40 | 0 |

## auto, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 5/12 | 12/12 | 4/4 | - | 7/12 | - | 0 | 0 | 0 | 1/12, 8030 ms | 37 | 8 |
| all | 12 | 12/12 | 5/12 | 12/12 | 4/4 | - | 7/12 | - | 0 | 0 | 0 | 1/12, 8030 ms | 37 | 8 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 8 / 8 |

Missing proposal traces: 0; gateway proposals: 8; prefetch diagnostics: {'admitted': 8, 'rejected': 0, 'executed': 8}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 5 |
| external | 7 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 8 / 8 / 0 | 7891 | 10410 |
| measured/local/qwen3-0.6b/final | 5 / 5 / 0 | 38 | 40 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 86 | 103 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 238 | 260 |
| measured/local/qwen3-0.6b/skill | 16 / 16 / 0 | 450 | 962 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 8 / 8 |

Missing proposal traces: 0; gateway proposals: 8; prefetch diagnostics: {'admitted': 8, 'rejected': 0, 'executed': 8}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 5 |
| external | 7 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 8 / 8 / 0 | 7891 | 10410 |
| measured/local/qwen3-0.6b/final | 5 / 5 / 0 | 38 | 40 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 86 | 103 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 238 | 260 |
| measured/local/qwen3-0.6b/skill | 16 / 16 / 0 | 450 | 962 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 16 / 37 | 986 | 1191 |
| wasted_local_ms | 7 / 7 | 2345 | 3413 |

generations per completed run: 3.75; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'local/routing': 12, 'local/skill': 16, 'local/final': 5, 'external/external': 8, 'local/repair': 4}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 10940 | 0 | 911.6666666666666 |
| output_tokens (known token subtotal) | 493 | 0 | 41.083333333333336 |

External calls: 8 across all attempts; successful tasks: 12; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 12/12 | 0 |

## external_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 0/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0/12, 0 ms | 0 | 24 |
| complex | 10 | 2/10 | - | 10/10 | 32/32 | - | - | - | 0 | 0 | 8 | 0/2, 0 ms | 0 | 20 |
| ambiguous | 6 | 0/6 | - | 0/6 | 9/9 | - | - | - | 0 | 0 | 0 | 0/6, 0 ms | 0 | 11 |
| mutation | 6 | 1/6 | - | 1/6 | 7/7 | - | - | 1/6 | 0 | 0 | 0 | 0/6, 0 ms | 0 | 13 |
| adversarial | 6 | 5/6 | - | 6/6 | 4/4 | - | - | - | 0 | 0 | 0 | 0/6, 0 ms | 0 | 10 |
| all | 40 | 20/40 | 0/12 | 29/40 | 64/64 | - | - | 1/6 | 0 | 0 | 8 | 0/32, 0 ms | 0 | 78 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 24 / 24 / 0 | 8451 | 10908 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 12 | 0 | 0 | None | None | None |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 32 / 32 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 0 |
| external | 2 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 20 / 20 / 0 | 8375 | 11040 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 10 | 0 | 0 | None | None | None |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 5 / 9 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 0 |
| external | 6 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 11 / 11 / 0 | 8568 | 9635 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 6 | 0 | 0 | None | None | None |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 7 / 7 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 0 |
| external | 6 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 13 / 13 / 0 | 9005 | 11478 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 6 | 0 | 0 | None | None | None |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 0 |
| external | 6 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 10 / 10 / 0 | 8961 | 10810 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 6 | 0 | 0 | None | None | None |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 60 / 64 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 32 |
| local | 0 |
| external | 32 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 78 / 78 / 0 | 8605 | 11433 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 40 | 0 | 0 | None | None | None |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 0 / 0 | - | - |
| wasted_local_ms | 32 / 32 | 0 | 0 |

generations per completed run: 1.9375; completed: 32; measured: 32; missing: 0; actor/purpose totals: {'external/external': 62}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 105183 | 8 | None |
| output_tokens (known token subtotal) | 8342 | 8 | None |

External calls: 78 across all attempts; successful tasks: 20; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 20/40 | 0 |

## external_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 0/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0/12, 0 ms | 0 | 24 |
| all | 12 | 12/12 | 0/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0/12, 0 ms | 0 | 24 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 24 / 24 / 0 | 8097 | 10855 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 12 | 0 | 0 | None | None | None |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 24 / 24 / 0 | 8097 | 10855 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/unknown/unknown | 0 | 0 | 12 | 0 | 0 | None | None | None |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 0 / 0 | - | - |
| wasted_local_ms | 12 / 12 | 0 | 0 |

generations per completed run: 2.0; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'external/external': 24}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 34992 | 0 | 2916.0 |
| output_tokens (known token subtotal) | 1918 | 0 | 159.83333333333334 |

External calls: 24 across all attempts; successful tasks: 12; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 12/12 | 0 |

## local_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 6/12 | 6/12 | 11/12 | 4/4 | - | - | - | 0 | 0 | 5 | 0/7, 0 ms | 31 | 0 |
| complex | 10 | 0/10 | - | 1/10 | 0/0 | - | - | - | 0 | 0 | 9 | 0/1, 0 ms | 11 | 0 |
| ambiguous | 6 | 3/6 | - | 4/6 | 2/2 | - | - | - | 0 | 0 | 3 | 0/3, 0 ms | 15 | 0 |
| mutation | 6 | 0/6 | - | 0/6 | 2/2 | - | - | 0/6 | 0 | 0 | 6 | 0/0, 0 ms | 12 | 0 |
| adversarial | 6 | 4/6 | - | 4/6 | 1/1 | - | - | - | 0 | 0 | 3 | 0/1, 0 ms | 8 | 0 |
| all | 40 | 13/40 | 6/12 | 20/40 | 9/9 | - | - | 0/6 | 0 | 0 | 26 | 0/12, 0 ms | 77 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 7 / 7 |

Missing proposal traces: 0; gateway proposals: 7; prefetch diagnostics: {'admitted': 7, 'rejected': 0, 'executed': 7}.

| answer owner | completed runs |
|---|---|
| eligible | 7 |
| local | 7 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 63 | 87 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 236 | 290 |
| measured/local/qwen3-0.6b/skill | 15 / 15 / 0 | 210 | 239 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 0 / 0 |
| prefetch accuracy | 1 / 1 |

Missing proposal traces: 0; gateway proposals: 1; prefetch diagnostics: {'admitted': 1, 'rejected': 0, 'executed': 1}.

| answer owner | completed runs |
|---|---|
| eligible | 1 |
| local | 1 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 201 | 247 |
| measured/local/qwen3-0.6b/skill | 1 / 1 / 0 | 190 | 190 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 2 |
| prefetch accuracy | 3 / 3 |

Missing proposal traces: 0; gateway proposals: 3; prefetch diagnostics: {'admitted': 3, 'rejected': 0, 'executed': 3}.

| answer owner | completed runs |
|---|---|
| eligible | 3 |
| local | 3 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 87 | 138 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 233 | 284 |
| measured/local/qwen3-0.6b/skill | 7 / 7 / 0 | 221 | 454 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 2 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 0 |
| local | 0 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 69 | 115 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 76 | 275 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 235 | 242 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 0 / 1 |
| prefetch accuracy | 2 / 3 |

Missing proposal traces: 0; gateway proposals: 3; prefetch diagnostics: {'admitted': 3, 'rejected': 0, 'executed': 5}.

| answer owner | completed runs |
|---|---|
| eligible | 1 |
| local | 1 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 214 | 237 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 179 | 192 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 8 / 9 |
| prefetch accuracy | 13 / 14 |

Missing proposal traces: 0; gateway proposals: 14; prefetch diagnostics: {'admitted': 14, 'rejected': 0, 'executed': 16}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 12 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 8 / 8 / 0 | 77 | 138 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 222 | 276 |
| measured/local/qwen3-0.6b/skill | 29 / 29 / 0 | 215 | 248 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 48 / 77 | 1003 | 1158 |
| wasted_local_ms | 26 / 26 | 434 | 2933 |

generations per completed run: 2.1666666666666665; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'local/routing': 12, 'local/skill': 14}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 13; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 13/40 | 0 |

## local_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 5/12 | 5/12 | 12/12 | 4/4 | - | - | - | 0 | 0 | 7 | 0/5, 0 ms | 37 | 0 |
| all | 12 | 5/12 | 5/12 | 12/12 | 4/4 | - | - | - | 0 | 0 | 7 | 0/5, 0 ms | 37 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 8 / 8 |

Missing proposal traces: 0; gateway proposals: 8; prefetch diagnostics: {'admitted': 8, 'rejected': 0, 'executed': 8}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 5 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 5 / 5 / 0 | 39 | 41 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 85 | 87 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 234 | 272 |
| measured/local/qwen3-0.6b/skill | 16 / 16 / 0 | 207 | 242 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 8 / 8 |

Missing proposal traces: 0; gateway proposals: 8; prefetch diagnostics: {'admitted': 8, 'rejected': 0, 'executed': 8}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 5 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 5 / 5 / 0 | 39 | 41 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 85 | 87 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 234 | 272 |
| measured/local/qwen3-0.6b/skill | 16 / 16 / 0 | 207 | 242 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 16 / 37 | 948 | 1202 |
| wasted_local_ms | 7 / 7 | 1589 | 2474 |

generations per completed run: 2.4; completed: 5; measured: 5; missing: 0; actor/purpose totals: {'local/routing': 5, 'local/skill': 5, 'local/final': 2}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 5; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 5/12 | 0 |

## Section 12.3 targets (English simple subset)

- auto: local_completion 6/12 (target 80%, not met); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- external_only: local_completion 0/12 (target 80%, not met); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- local_only: local_completion 6/12 (target 80%, not met); tool_correctness 11/12 (target 95%, not met); missed confirmations 0 (met)

# M7-C5

## auto, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 0/0 | 12/12 | 12/12 | - | 12/12 | - | 0 | 0 | 0 | 3/12, 28847 ms | 0 | 27 |
| complex | 10 | 4/10 | - | 10/10 | 57/57 | 0/10 | - | - | 0 | 0 | 6 | 0/4, 0 ms | 0 | 41 |
| ambiguous | 6 | 4/6 | - | 4/6 | 7/7 | - | 6/6 | - | 0 | 0 | 1 | 0/5, 0 ms | 0 | 15 |
| mutation | 6 | 6/6 | - | 6/6 | 12/12 | - | 6/6 | 6/6 | 0 | 0 | 0 | 0/4, 0 ms | 0 | 16 |
| adversarial | 6 | 5/6 | - | 6/6 | 4/4 | 0/1 | 5/5 | - | 0 | 0 | 1 | 0/5, 0 ms | 0 | 14 |
| all | 40 | 31/40 | 0/0 | 38/40 | 92/92 | 0/11 | 29/29 | 6/6 | 0 | 0 | 8 | 3/30, 28847 ms | 0 | 113 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 27 / 27 / 0 | 8143 | 10811 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.5833333333333334 | 0.5833333333333334 | 0.5833333333333334 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 57 / 57 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 4 |
| local | 0 |
| external | 4 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 41 / 41 / 0 | 8388 | 9961 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/1/in_process | 10 | 0 | 0 | 0 | 10 | 0.1 | 0.1 | 0.1 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 6 / 7 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 0 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 15 / 15 / 0 | 8249 | 9886 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 4 |
| local | 0 |
| external | 4 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 16 / 16 / 0 | 8262 | 10583 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/1/in_process | 6 | 0 | 0 | 0 | 6 | 1.0 | 1.0 | 1.0 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 0 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 13 / 14 / 1 | 8134 | 9173 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.16666666666666666 | 0.16666666666666666 | 0.16666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 91 / 92 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 30 |
| local | 0 |
| external | 30 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 112 / 113 / 1 | 8177 | 10442 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/1/in_process | 40 | 0 | 0 | 0 | 40 | 0.475 | 0.475 | 0.475 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 0 / 0 | - | - |
| wasted_local_ms | 30 / 30 | 0 | 0 |

generations per completed run: 2.2666666666666666; completed: 30; measured: 30; missing: 0; actor/purpose totals: {'external/external': 68}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 146524 | 7 | None |
| output_tokens (known token subtotal) | 10688 | 7 | None |

External calls: 113 across all attempts; successful tasks: 31; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 31/40 | 0 |

## auto, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 0/0 | 12/12 | 12/12 | - | 12/12 | - | 0 | 0 | 0 | 2/12, 20259 ms | 0 | 26 |
| all | 12 | 12/12 | 0/0 | 12/12 | 12/12 | - | 12/12 | - | 0 | 0 | 0 | 2/12, 20259 ms | 0 | 26 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 26 / 26 / 0 | 8289 | 11828 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.5833333333333334 | 0.5833333333333334 | 0.5833333333333334 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 26 / 26 / 0 | 8289 | 11828 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.5833333333333334 | 0.5833333333333334 | 0.5833333333333334 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 0 / 0 | - | - |
| wasted_local_ms | 12 / 12 | 0 | 0 |

generations per completed run: 2.1666666666666665; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'external/external': 26}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 32636 | 0 | 2719.6666666666665 |
| output_tokens (known token subtotal) | 1659 | 0 | 138.25 |

External calls: 26 across all attempts; successful tasks: 12; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 12/12 | 0 |

## external_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 0/0 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0/12, 0 ms | 0 | 24 |
| complex | 10 | 5/10 | - | 10/10 | 57/57 | - | - | - | 0 | 0 | 5 | 0/5, 0 ms | 0 | 43 |
| ambiguous | 6 | 0/6 | - | 0/6 | 8/8 | - | - | - | 0 | 0 | 0 | 0/6, 0 ms | 0 | 11 |
| mutation | 6 | 0/6 | - | 0/6 | 6/6 | - | - | 0/6 | 0 | 0 | 0 | 0/6, 0 ms | 0 | 12 |
| adversarial | 6 | 5/6 | - | 6/6 | 4/4 | - | - | - | 0 | 0 | 0 | 0/6, 0 ms | 0 | 10 |
| all | 40 | 22/40 | 0/0 | 28/40 | 87/87 | - | - | 0/6 | 0 | 0 | 5 | 0/35, 0 ms | 0 | 100 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 24 / 24 / 0 | 7198 | 10123 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/unknown/unknown | 0 | 0 | 12 | 0 | 0 | None | None | None |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 57 / 57 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 0 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 43 / 43 / 0 | 8525 | 11957 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/unknown/unknown | 0 | 0 | 10 | 0 | 0 | None | None | None |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 5 / 8 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 0 |
| external | 6 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 11 / 11 / 0 | 8057 | 11829 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/unknown/unknown | 0 | 0 | 6 | 0 | 0 | None | None | None |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 6 / 6 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 0 |
| external | 6 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 12 / 12 / 0 | 8609 | 11360 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/unknown/unknown | 0 | 0 | 6 | 0 | 0 | None | None | None |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 4 / 4 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 6 |
| local | 0 |
| external | 6 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 10 / 10 / 0 | 8066 | 10652 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/unknown/unknown | 0 | 0 | 6 | 0 | 0 | None | None | None |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 84 / 87 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 35 |
| local | 0 |
| external | 35 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 100 / 100 / 0 | 8066 | 11360 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/unknown/unknown | 0 | 0 | 40 | 0 | 0 | None | None | None |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 0 / 0 | - | - |
| wasted_local_ms | 35 / 35 | 0 | 0 |

generations per completed run: 2.085714285714286; completed: 35; measured: 35; missing: 0; actor/purpose totals: {'external/external': 73}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 149811 | 4 | None |
| output_tokens (known token subtotal) | 10929 | 4 | None |

External calls: 100 across all attempts; successful tasks: 22; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 22/40 | 0 |

## external_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 0/0 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0/12, 0 ms | 0 | 24 |
| all | 12 | 12/12 | 0/0 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0/12, 0 ms | 0 | 24 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 24 / 24 / 0 | 7699 | 9977 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/unknown/unknown | 0 | 0 | 12 | 0 | 0 | None | None | None |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 0 |
| external | 12 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 24 / 24 / 0 | 7699 | 9977 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| example_match/unknown/unknown | 0 | 0 | 12 | 0 | 0 | None | None | None |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 0 / 0 | - | - |
| wasted_local_ms | 12 / 12 | 0 | 0 |

generations per completed run: 2.0; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'external/external': 24}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 34970 | 0 | 2914.1666666666665 |
| output_tokens (known token subtotal) | 1940 | 0 | 161.66666666666666 |

External calls: 24 across all attempts; successful tasks: 12; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 12/12 | 0 |

## Section 12.3 targets (English simple subset)

- auto: local_completion 0/0 (target 80%, not applicable); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- external_only: local_completion 0/0 (target 80%, not applicable); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)

# M7-C6

## auto, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 11/12 | 4/12 | 12/12 | 12/12 | - | 7/12 | - | 0 | 0 | 0 | 9/12, 86318 ms | 45 | 17 |
| complex | 10 | 2/10 | - | 10/10 | 50/50 | 1/10 | - | - | 0 | 0 | 6 | 0/4, 0 ms | 13 | 42 |
| ambiguous | 6 | 5/6 | - | 5/6 | 6/6 | - | 4/6 | - | 0 | 0 | 1 | 1/5, 9250 ms | 21 | 9 |
| mutation | 6 | 2/6 | - | 2/6 | 8/8 | - | 6/6 | 2/6 | 0 | 0 | 0 | 1/5, 7516 ms | 12 | 12 |
| adversarial | 6 | 4/6 | - | 4/6 | 5/5 | 0/1 | 3/5 | - | 0 | 0 | 2 | 1/2, 12270 ms | 11 | 15 |
| all | 40 | 24/40 | 4/12 | 33/40 | 81/81 | 1/11 | 20/29 | 2/6 | 0 | 0 | 9 | 12/28, 115354 ms | 102 | 95 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 5 |
| external | 7 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 17 / 17 / 0 | 8806 | 16731 |
| measured/local/qwen3-0.6b/final | 7 / 7 / 0 | 446 | 481 |
| measured/local/qwen3-0.6b/repair | 4 / 4 / 0 | 81 | 85 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 235 | 270 |
| measured/local/qwen3-0.6b/skill | 22 / 22 / 0 | 231 | 571 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 50 / 50 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 4 |
| local | 1 |
| external | 3 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 42 / 42 / 0 | 7521 | 10369 |
| measured/local/qwen3-0.6b/final | 1 / 1 / 0 | 446 | 446 |
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 201 | 280 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 208 | 456 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 6 / 6 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 2 |
| external | 3 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 9 / 9 / 0 | 8886 | 10322 |
| measured/local/qwen3-0.6b/final | 3 / 3 / 0 | 468 | 488 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 71 | 136 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 233 | 361 |
| measured/local/qwen3-0.6b/skill | 10 / 10 / 0 | 247 | 681 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 8 / 8 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 0 |
| external | 5 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 12 / 12 / 0 | 8201 | 10301 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 66 | 122 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 76 | 229 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 243 | 488 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 3 / 5 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 0 |
| external | 2 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 13 / 15 / 2 | 8491 | 10925 |
| measured/local/qwen3-0.6b/repair | 1 / 1 / 0 | 111 | 111 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 210 | 273 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 444 | 489 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 79 / 81 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 28 |
| local | 8 |
| external | 20 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 93 / 95 / 2 | 8307 | 10547 |
| measured/local/qwen3-0.6b/final | 11 / 11 / 0 | 446 | 488 |
| measured/local/qwen3-0.6b/repair | 9 / 9 / 0 | 83 | 136 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 218 | 273 |
| measured/local/qwen3-0.6b/skill | 42 / 42 / 0 | 369 | 612 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 59 / 102 | 996 | 1196 |
| wasted_local_ms | 20 / 20 | 584 | 2968 |

generations per completed run: 4.535714285714286; completed: 28; measured: 28; missing: 0; actor/purpose totals: {'local/routing': 28, 'external/external': 46, 'local/skill': 35, 'local/final': 11, 'local/repair': 7}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 123227 | 7 | None |
| output_tokens (known token subtotal) | 10464 | 7 | None |

External calls: 95 across all attempts; successful tasks: 24; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 24/40 | 0 |

## auto, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 2/12 | 12/12 | 12/12 | - | 10/12 | - | 0 | 0 | 0 | 4/12, 38409 ms | 48 | 14 |
| all | 12 | 12/12 | 2/12 | 12/12 | 12/12 | - | 10/12 | - | 0 | 0 | 0 | 4/12, 38409 ms | 48 | 14 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 2 |
| external | 10 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 14 / 14 / 0 | 7900 | 11142 |
| measured/local/qwen3-0.6b/final | 6 / 6 / 0 | 433 | 460 |
| measured/local/qwen3-0.6b/repair | 6 / 6 / 0 | 88 | 96 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 257 | 348 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 263 | 581 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 12 |
| local | 2 |
| external | 10 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/external/glm-4.6/external | 14 / 14 / 0 | 7900 | 11142 |
| measured/local/qwen3-0.6b/final | 6 / 6 / 0 | 433 | 460 |
| measured/local/qwen3-0.6b/repair | 6 / 6 / 0 | 88 | 96 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 257 | 348 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 263 | 581 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 24 / 48 | 360 | 1421 |
| wasted_local_ms | 10 / 10 | 2697 | 3144 |

generations per completed run: 5.166666666666667; completed: 12; measured: 12; missing: 0; actor/purpose totals: {'local/routing': 12, 'local/skill': 24, 'local/final': 6, 'external/external': 14, 'local/repair': 6}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 16602 | 0 | 1383.5 |
| output_tokens (known token subtotal) | 1531 | 0 | 127.58333333333333 |

External calls: 14 across all attempts; successful tasks: 12; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 12/12 | 0 |

## local_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 4/12 | 4/12 | 11/12 | 11/11 | - | - | - | 0 | 0 | 7 | 0/5, 0 ms | 45 | 0 |
| complex | 10 | 0/10 | - | 1/10 | 1/1 | - | - | - | 0 | 0 | 9 | 0/1, 0 ms | 13 | 0 |
| ambiguous | 6 | 2/6 | - | 4/6 | 5/5 | - | - | - | 0 | 0 | 4 | 0/2, 0 ms | 21 | 0 |
| mutation | 6 | 0/6 | - | 0/6 | 2/2 | - | - | 0/6 | 0 | 0 | 6 | 0/0, 0 ms | 12 | 0 |
| adversarial | 6 | 4/6 | - | 4/6 | 3/3 | - | - | - | 0 | 0 | 4 | 0/0, 0 ms | 11 | 0 |
| all | 40 | 10/40 | 4/12 | 20/40 | 22/22 | - | - | 0/6 | 0 | 0 | 30 | 0/8, 0 ms | 102 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 40 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 11 / 11 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 5 |
| local | 5 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 8 / 8 / 0 | 201 | 518 |
| measured/local/qwen3-0.6b/repair | 3 / 3 / 0 | 86 | 88 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 232 | 272 |
| measured/local/qwen3-0.6b/skill | 22 / 22 / 0 | 217 | 430 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 0.9166666666666666 | 0.9166666666666666 | 0.9166666666666666 |

### M7 observations: complex

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 1 / 1 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 1 |
| local | 1 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 1 / 1 / 0 | 182 | 182 |
| measured/local/qwen3-0.6b/routing | 10 / 10 / 0 | 206 | 250 |
| measured/local/qwen3-0.6b/skill | 2 / 2 / 0 | 201 | 237 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 10 | 1 | 0 | 0 | 10 | 0.8 | 0.8 | 0.8 |

### M7 observations: ambiguous

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 5 / 5 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 2 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 3 / 3 / 0 | 236 | 268 |
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 86 | 138 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 248 | 471 |
| measured/local/qwen3-0.6b/skill | 10 / 10 / 0 | 236 | 465 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 0 | 0 | 0 | 6 | 0.8333333333333334 | 0.8333333333333334 | 0.8333333333333334 |

### M7 observations: mutation

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 2 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 0 |
| local | 0 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 2 / 2 / 0 | 70 | 121 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 92 | 237 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 240 | 260 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 4 | 0 | 0 | 6 | 0.3333333333333333 | 0.3333333333333333 | 0.3333333333333333 |

### M7 observations: adversarial

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 2 / 3 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 0 |
| local | 0 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/repair | 1 / 1 / 0 | 107 | 107 |
| measured/local/qwen3-0.6b/routing | 6 / 6 / 0 | 216 | 343 |
| measured/local/qwen3-0.6b/skill | 4 / 4 / 0 | 161 | 469 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 6 | 1 | 0 | 0 | 6 | 0.6666666666666666 | 0.6666666666666666 | 0.6666666666666666 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 21 / 22 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 8 |
| local | 8 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 12 / 12 / 0 | 204 | 518 |
| measured/local/qwen3-0.6b/repair | 8 / 8 / 0 | 86 | 138 |
| measured/local/qwen3-0.6b/routing | 40 / 40 / 0 | 231 | 272 |
| measured/local/qwen3-0.6b/skill | 42 / 42 / 0 | 219 | 441 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 40 | 6 | 0 | 0 | 40 | 0.75 | 0.75 | 0.75 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 59 / 102 | 1025 | 1293 |
| wasted_local_ms | 30 / 30 | 563 | 2823 |

generations per completed run: 4.0; completed: 8; measured: 8; missing: 0; actor/purpose totals: {'local/routing': 8, 'local/skill': 16, 'local/final': 8}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 10; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 40 | 10/40 | 0 |

## local_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | corrections per completed run | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 2/12 | 2/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 10 | 0/2, 0 ms | 48 | 0 |
| all | 12 | 2/12 | 2/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 10 | 0/2, 0 ms | 48 | 0 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | runs |
|---|---|
| http_attempted | 0 |
| gateway_observed | 12 |
| unknown | 0 |

### M7 observations: simple

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 2 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 6 / 6 / 0 | 214 | 226 |
| measured/local/qwen3-0.6b/repair | 6 / 6 / 0 | 87 | 88 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 241 | 295 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 234 | 447 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

### M7 observations: all

| proposal metric | correct / observed |
|---|---|
| model proposal accuracy | 12 / 12 |
| prefetch accuracy | 0 / 0 |

Missing proposal traces: 0; gateway proposals: 0; prefetch diagnostics: {'admitted': 0, 'rejected': 0, 'executed': 0}.

| answer owner | completed runs |
|---|---|
| eligible | 2 |
| local | 2 |
| external | 0 |
| gateway | 0 |
| unknown | 0 |

| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| measured/local/qwen3-0.6b/final | 6 / 6 / 0 | 214 | 226 |
| measured/local/qwen3-0.6b/repair | 6 / 6 / 0 | 87 | 88 |
| measured/local/qwen3-0.6b/routing | 12 / 12 / 0 | 241 | 295 |
| measured/local/qwen3-0.6b/skill | 24 / 24 / 0 | 234 | 447 |

| attempted selector name/version/transport | attempted | failed | skipped | unobserved | scored | precision | recall | Jaccard |
|---|---|---|---|---|---|---|---|---|
| model_router/1/in_process | 12 | 0 | 0 | 0 | 12 | 1.0 | 1.0 | 1.0 |

| measured metric | n / eligible | p50 | p95 |
|---|---|---|---|
| prediction_error_ms | 24 / 48 | 259 | 1600 |
| wasted_local_ms | 10 / 10 | 2353 | 2626 |

generations per completed run: 4.0; completed: 2; measured: 2; missing: 0; actor/purpose totals: {'local/routing': 2, 'local/skill': 4, 'local/final': 2}.

| external cost | known total | missing calls | per successful task |
|---|---|---|---|
| input_tokens (known token subtotal) | 0 | 0 | 0.0 |
| output_tokens (known token subtotal) | 0 | 0 | 0.0 |

External calls: 0 across all attempts; successful tasks: 2; unobserved runs: 0. Unknown token counts stay null in the JSON export.

| effective answer stream | runs | task success | predicate_unmet |
|---|---|---|---|
| buffered | 12 | 2/12 | 0 |

## Section 12.3 targets (English simple subset)

- auto: local_completion 4/12 (target 80%, not met); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- local_only: local_completion 4/12 (target 80%, not met); tool_correctness 11/12 (target 95%, not met); missed confirmations 0 (met)

# M7-L0

## all: 120 samples, 0 timeouts (fraction 0.000), 51 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 120 | 533 | 2478 |
| first_model_action_ms | 90 | 427 | 510 |
| first_useful_content_ms | 39 | 529 | 2399 |
| first_answer_token_ms | 39 | 534 | 2403 |
| completion_ms | 19 | 2152 | 2482 |

## clarification: 30 samples, 0 timeouts (fraction 0.000), 10 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 421 | 514 |
| first_model_action_ms | 20 | 427 | 510 |
| first_useful_content_ms | 20 | 428 | 511 |
| first_answer_token_ms | 20 | 431 | 514 |
| completion_ms | 0 | - | - |

## routing: 30 samples, 0 timeouts (fraction 0.000), 0 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 437 | 986 |
| first_model_action_ms | 10 | 433 | 499 |
| first_useful_content_ms | 0 | - | - |
| first_answer_token_ms | 0 | - | - |
| completion_ms | 0 | - | - |

## simple: 60 samples, 0 timeouts (fraction 0.000), 41 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 60 | 2037 | 2538 |
| first_model_action_ms | 60 | 419 | 504 |
| first_useful_content_ms | 19 | 2145 | 2478 |
| first_answer_token_ms | 19 | 2151 | 2481 |
| completion_ms | 19 | 2152 | 2482 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | samples |
|---|---|
| http_attempted | 0 |
| gateway_observed | 120 |
| unknown | 0 |

Hypothesis: p95 first_model_action_ms <= 500 ms: not met (p95 510 ms over 90 values, 120 samples, 0 timeouts).


| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| cold/local/qwen3-0.6b/final | 1 / 1 / 0 | 439 | 439 |
| cold/local/qwen3-0.6b/routing | 1 / 1 / 0 | 198 | 198 |
| cold/local/qwen3-0.6b/skill | 2 / 2 / 0 | 201 | 406 |
| measured/local/qwen3-0.6b/final | 22 / 22 / 0 | 227 | 424 |
| measured/local/qwen3-0.6b/repair | 28 / 28 / 0 | 73 | 87 |
| measured/local/qwen3-0.6b/routing | 120 / 120 / 0 | 224 | 283 |
| measured/local/qwen3-0.6b/skill | 100 / 100 / 0 | 229 | 485 |
| runner_warmup/local/qwen3-0.6b/final | 1 / 1 / 0 | 206 | 206 |
| runner_warmup/local/qwen3-0.6b/routing | 3 / 3 / 0 | 58 | 176 |
| runner_warmup/local/qwen3-0.6b/skill | 2 / 2 / 0 | 203 | 205 |
| startup_warmup/local/qwen3-0.6b/routing | 1 / 1 / 0 | 709 | 709 |
| startup_warmup/local/qwen3-0.6b/skill | 1 / 1 / 0 | 574 | 574 |

# M7-L1

Early response headers were on: first_transport_ms is the header time, the arrival of the status line at acceptance; it is not first useful content (spec 0.32 section 7.3).

## all: 120 samples, 0 timeouts (fraction 0.000), 51 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms (header time) | 120 | 3 | 5 |
| first_model_action_ms | 90 | 433 | 495 |
| first_useful_content_ms | 39 | 496 | 2336 |
| first_answer_token_ms | 39 | 499 | 2343 |
| completion_ms | 19 | 2026 | 2394 |

## clarification: 30 samples, 0 timeouts (fraction 0.000), 10 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms (header time) | 30 | 3 | 5 |
| first_model_action_ms | 20 | 441 | 466 |
| first_useful_content_ms | 20 | 441 | 467 |
| first_answer_token_ms | 20 | 445 | 469 |
| completion_ms | 0 | - | - |

## routing: 30 samples, 0 timeouts (fraction 0.000), 0 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms (header time) | 30 | 3 | 5 |
| first_model_action_ms | 10 | 426 | 467 |
| first_useful_content_ms | 0 | - | - |
| first_answer_token_ms | 0 | - | - |
| completion_ms | 0 | - | - |

## simple: 60 samples, 0 timeouts (fraction 0.000), 41 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms (header time) | 60 | 3 | 5 |
| first_model_action_ms | 60 | 426 | 503 |
| first_useful_content_ms | 19 | 2022 | 2386 |
| first_answer_token_ms | 19 | 2026 | 2393 |
| completion_ms | 19 | 2026 | 2394 |

HTTP selector-internal model cost is not verified; model calls and tokens are gateway-observed only.

| selector provenance | samples |
|---|---|
| http_attempted | 0 |
| gateway_observed | 120 |
| unknown | 0 |

Hypothesis: p95 first_model_action_ms <= 500 ms: not met (p95 495 ms over 90 values, 120 samples, 0 timeouts).


| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |
|---|---|---|---|
| cold/local/qwen3-0.6b/final | 1 / 1 / 0 | 433 | 433 |
| cold/local/qwen3-0.6b/routing | 1 / 1 / 0 | 215 | 215 |
| cold/local/qwen3-0.6b/skill | 2 / 2 / 0 | 198 | 428 |
| measured/local/qwen3-0.6b/final | 22 / 22 / 0 | 229 | 446 |
| measured/local/qwen3-0.6b/repair | 28 / 28 / 0 | 75 | 91 |
| measured/local/qwen3-0.6b/routing | 120 / 120 / 0 | 226 | 277 |
| measured/local/qwen3-0.6b/skill | 100 / 100 / 0 | 229 | 483 |
| runner_warmup/local/qwen3-0.6b/final | 1 / 1 / 0 | 199 | 199 |
| runner_warmup/local/qwen3-0.6b/routing | 3 / 3 / 0 | 66 | 173 |
| runner_warmup/local/qwen3-0.6b/skill | 2 / 2 / 0 | 197 | 208 |
| startup_warmup/local/qwen3-0.6b/routing | 1 / 1 / 0 | 713 | 713 |
| startup_warmup/local/qwen3-0.6b/skill | 1 / 1 / 0 | 568 | 568 |

## Historical corrections versus current

These are confounded revision/date comparisons, not isolated causal evidence.

| source | mode | language | class | corrections | completed | missing | summed ms |
|---|---|---|---|---|---|---|---|
| historical | auto | en | all | 10 | 27 | 0 | 85041 |
| historical | auto | en | simple | 6 | 12 | 0 | 50480 |
| historical | auto | en | complex | 0 | 3 | 0 | 0 |
| historical | auto | en | ambiguous | 1 | 5 | 0 | 6008 |
| historical | auto | en | mutation | 2 | 5 | 0 | 17358 |
| historical | auto | en | adversarial | 1 | 2 | 0 | 11195 |
| historical | auto | zh | all | 7 | 12 | 0 | 57420 |
| historical | auto | zh | simple | 7 | 12 | 0 | 57420 |
| historical | external_only | en | all | 0 | 33 | 0 | 0 |
| historical | external_only | en | simple | 0 | 12 | 0 | 0 |
| historical | external_only | en | complex | 0 | 3 | 0 | 0 |
| historical | external_only | en | ambiguous | 0 | 6 | 0 | 0 |
| historical | external_only | en | mutation | 0 | 6 | 0 | 0 |
| historical | external_only | en | adversarial | 0 | 6 | 0 | 0 |
| historical | external_only | zh | all | 0 | 12 | 0 | 0 |
| historical | external_only | zh | simple | 0 | 12 | 0 | 0 |
| historical | local_only | en | all | 0 | 8 | 0 | 0 |
| historical | local_only | en | simple | 0 | 5 | 0 | 0 |
| historical | local_only | en | complex | 0 | 1 | 0 | 0 |
| historical | local_only | en | ambiguous | 0 | 2 | 0 | 0 |
| historical | local_only | en | mutation | 0 | 0 | 0 | 0 |
| historical | local_only | en | adversarial | 0 | 0 | 0 | 0 |
| historical | local_only | zh | all | 0 | 3 | 0 | 0 |
| historical | local_only | zh | simple | 0 | 3 | 0 | 0 |
| M7-C0 | auto | en | all | 3 | 32 | 0 | 25019 |
| M7-C0 | auto | en | simple | 1 | 12 | 0 | 6681 |
| M7-C0 | auto | en | complex | 0 | 8 | 0 | 0 |
| M7-C0 | auto | en | ambiguous | 0 | 5 | 0 | 0 |
| M7-C0 | auto | en | mutation | 1 | 5 | 0 | 6080 |
| M7-C0 | auto | en | adversarial | 1 | 2 | 0 | 12258 |
| M7-C0 | auto | zh | all | 0 | 12 | 0 | 0 |
| M7-C0 | auto | zh | simple | 0 | 12 | 0 | 0 |
| M7-C0 | external_only | en | all | 0 | 37 | 0 | 0 |
| M7-C0 | external_only | en | simple | 0 | 12 | 0 | 0 |
| M7-C0 | external_only | en | complex | 0 | 7 | 0 | 0 |
| M7-C0 | external_only | en | ambiguous | 0 | 6 | 0 | 0 |
| M7-C0 | external_only | en | mutation | 0 | 6 | 0 | 0 |
| M7-C0 | external_only | en | adversarial | 0 | 6 | 0 | 0 |
| M7-C0 | external_only | zh | all | 0 | 12 | 0 | 0 |
| M7-C0 | external_only | zh | simple | 0 | 12 | 0 | 0 |
| M7-C0 | local_only | en | all | 0 | 8 | 0 | 0 |
| M7-C0 | local_only | en | simple | 0 | 5 | 0 | 0 |
| M7-C0 | local_only | en | complex | 0 | 1 | 0 | 0 |
| M7-C0 | local_only | en | ambiguous | 0 | 2 | 0 | 0 |
| M7-C0 | local_only | en | mutation | 0 | 0 | 0 | 0 |
| M7-C0 | local_only | en | adversarial | 0 | 0 | 0 | 0 |
| M7-C0 | local_only | zh | all | 0 | 3 | 0 | 0 |
| M7-C0 | local_only | zh | simple | 0 | 3 | 0 | 0 |

## Section 12.3 hypotheses

| hypothesis | status | evidence / limits |
|---|---|---|
| draft_local_completion | met | {"status": "met", "evidence": ["M7-C0", "M7-C1"], "population": "auto/en/simple", "values": [0.3333333333333333, 0.5, 0.9166666666666666, 0.9166666666666666]} |
| prefetch_and_many_generations | met | {"status": "met", "evidence": ["M7-C0", "M7-C2", "M7-C3"], "population": "auto/complex, languages separate", "values": {"en": [5.125, 3.1666666666666665, 3.0]}, "not_applicable": {"zh": "the frozen corpus has no zh complex cases"}} |
| criteria_corrections | met | {"status": "met", "evidence": ["M7-C6", "M7-C0"], "scope": "criteria and named correction bundle by explicit revision", "values": {"en": [0.42857142857142855, 0.09375], "zh": [0.3333333333333333, 0.0]}} |
| early_headers | not measured | {"status": "not measured", "evidence": ["M7-L1"], "eligible": 39, "requests": 120, "scope": "header arrival before first answer token; generation-start ordering not measured", "header_before_answer_token": "met"} |
| none_live_streaming | not measured | {"status": "not measured", "evidence": []} |
| unsuitable_device_aware | not measured | {"status": "not measured", "evidence": []} |
| example_matcher_jaccard | not measured | {"status": "not measured", "evidence": []} |
| tool_narrowing_precision | not measured | {"status": "not measured", "evidence": []} |
| external_cost_next_to_success | met | {"status": "met", "evidence": ["M7-C0", "M7-C1", "M7-C2", "M7-C3", "M7-C4", "M7-C5", "M7-C6"]} |

## Retained attempt cost

| cell | reused | cost (known subtotals and missing counts) |
|---|---|---|
| M7-C0 | True | {'physical_attempts': 1, 'calls': 218, 'successful_tasks': 89, 'unreadable_attempts': 0, 'calls_per_successful_task': 2.449438202247191, 'input_tokens_known_total': 326684, 'input_tokens_missing_calls': 5, 'input_tokens_per_successful_task': None, 'output_tokens_known_total': 27409, 'output_tokens_missing_calls': 5, 'output_tokens_per_successful_task': None} |
| M7-C1 | False | {'physical_attempts': 1, 'calls': 91, 'successful_tasks': 52, 'unreadable_attempts': 0, 'calls_per_successful_task': 1.75, 'input_tokens_known_total': 121456, 'input_tokens_missing_calls': 9, 'input_tokens_per_successful_task': None, 'output_tokens_known_total': 8489, 'output_tokens_missing_calls': 9, 'output_tokens_per_successful_task': None} |
| M7-C2 | False | {'physical_attempts': 1, 'calls': 75, 'successful_tasks': 50, 'unreadable_attempts': 0, 'calls_per_successful_task': 1.5, 'input_tokens_known_total': 97836, 'input_tokens_missing_calls': 7, 'input_tokens_per_successful_task': None, 'output_tokens_known_total': 9492, 'output_tokens_missing_calls': 7, 'output_tokens_per_successful_task': None} |
| M7-C3 | False | {'physical_attempts': 1, 'calls': 77, 'successful_tasks': 50, 'unreadable_attempts': 0, 'calls_per_successful_task': 1.54, 'input_tokens_known_total': 112102, 'input_tokens_missing_calls': 5, 'input_tokens_per_successful_task': None, 'output_tokens_known_total': 8905, 'output_tokens_missing_calls': 5, 'output_tokens_per_successful_task': None} |
| M7-C4 | False | {'physical_attempts': 1, 'calls': 165, 'successful_tasks': 88, 'unreadable_attempts': 0, 'calls_per_successful_task': 1.875, 'input_tokens_known_total': 221871, 'input_tokens_missing_calls': 16, 'input_tokens_per_successful_task': None, 'output_tokens_known_total': 17043, 'output_tokens_missing_calls': 16, 'output_tokens_per_successful_task': None} |
| M7-C5 | True | {'physical_attempts': 1, 'calls': 263, 'successful_tasks': 77, 'unreadable_attempts': 0, 'calls_per_successful_task': 3.4155844155844157, 'input_tokens_known_total': 363941, 'input_tokens_missing_calls': 11, 'input_tokens_per_successful_task': None, 'output_tokens_known_total': 25216, 'output_tokens_missing_calls': 11, 'output_tokens_per_successful_task': None} |
| M7-C6 | False | {'physical_attempts': 1, 'calls': 109, 'successful_tasks': 48, 'unreadable_attempts': 0, 'calls_per_successful_task': 2.2708333333333335, 'input_tokens_known_total': 139829, 'input_tokens_missing_calls': 7, 'input_tokens_per_successful_task': None, 'output_tokens_known_total': 11995, 'output_tokens_missing_calls': 7, 'output_tokens_per_successful_task': None} |
| M7-L0 | False | {'physical_attempts': 1, 'calls': 0, 'successful_tasks': 19, 'unreadable_attempts': 0, 'calls_per_successful_task': 0.0, 'input_tokens_known_total': 0, 'input_tokens_missing_calls': 0, 'input_tokens_per_successful_task': 0.0, 'output_tokens_known_total': 0, 'output_tokens_missing_calls': 0, 'output_tokens_per_successful_task': 0.0} |
| M7-L1 | False | {'physical_attempts': 1, 'calls': 0, 'successful_tasks': 19, 'unreadable_attempts': 0, 'calls_per_successful_task': 0.0, 'input_tokens_known_total': 0, 'input_tokens_missing_calls': 0, 'input_tokens_per_successful_task': 0.0, 'output_tokens_known_total': 0, 'output_tokens_missing_calls': 0, 'output_tokens_per_successful_task': 0.0} |

Reporting acceptance: passed. Historical latency sources retain their measured revisions and protocol; see JSON historical_latency.
