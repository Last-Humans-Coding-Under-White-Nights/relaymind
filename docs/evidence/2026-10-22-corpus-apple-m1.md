# Correctness corpus: 2026-10-22-corpus-apple-m1

## auto, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 11/12 | 4/12 | 12/12 | 12/12 | - | 7/12 | - | 0 | 0 | 0 | 45 | 14 |
| complex | 10 | 2/10 | - | 10/10 | 49/49 | 1/10 | - | - | 0 | 0 | 7 | 13 | 46 |
| ambiguous | 6 | 4/6 | - | 5/6 | 6/6 | - | 4/6 | - | 0 | 0 | 1 | 21 | 9 |
| mutation | 6 | 3/6 | - | 3/6 | 9/9 | - | 6/6 | 3/6 | 0 | 0 | 0 | 12 | 14 |
| adversarial | 6 | 4/6 | - | 4/6 | 6/6 | 0/1 | 3/5 | - | 0 | 0 | 2 | 11 | 16 |
| all | 40 | 24/40 | 4/12 | 34/40 | 82/82 | 1/11 | 20/29 | 3/6 | 0 | 0 | 10 | 102 | 99 |

## auto, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 11/12 | 2/12 | 12/12 | 12/12 | - | 9/12 | - | 0 | 0 | 0 | 48 | 16 |
| all | 12 | 11/12 | 2/12 | 12/12 | 12/12 | - | 9/12 | - | 0 | 0 | 0 | 48 | 16 |

## external_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 12/12 | 0/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0 | 24 |
| complex | 10 | 3/10 | - | 10/10 | 71/71 | - | - | - | 0 | 0 | 7 | 0 | 47 |
| ambiguous | 6 | 0/6 | - | 0/6 | 11/11 | - | - | - | 0 | 0 | 0 | 0 | 13 |
| mutation | 6 | 1/6 | - | 1/6 | 7/7 | - | - | 1/6 | 0 | 0 | 0 | 0 | 13 |
| adversarial | 6 | 4/6 | - | 6/6 | 4/4 | - | - | - | 0 | 0 | 0 | 0 | 10 |
| all | 40 | 20/40 | 0/12 | 29/40 | 105/105 | - | - | 1/6 | 0 | 0 | 7 | 0 | 107 |

## external_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 11/12 | 0/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0 | 24 |
| all | 12 | 11/12 | 0/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 0 | 0 | 24 |

## local_only, en

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 4/12 | 4/12 | 11/12 | 11/11 | - | - | - | 0 | 0 | 7 | 45 | 0 |
| complex | 10 | 0/10 | - | 1/10 | 1/1 | - | - | - | 0 | 0 | 9 | 13 | 0 |
| ambiguous | 6 | 2/6 | - | 4/6 | 5/5 | - | - | - | 0 | 0 | 4 | 21 | 0 |
| mutation | 6 | 0/6 | - | 0/6 | 2/2 | - | - | 0/6 | 0 | 0 | 6 | 12 | 0 |
| adversarial | 6 | 4/6 | - | 4/6 | 3/3 | - | - | - | 0 | 0 | 4 | 11 | 0 |
| all | 40 | 10/40 | 4/12 | 20/40 | 22/22 | - | - | 0/6 | 0 | 0 | 30 | 102 | 0 |

## local_only, zh

| class | runs | success | local completion | tool correct | schema-valid proposals | missed escalations | unnecessary escalations | confirmations reached | missed confirmations | timeouts | explicit failures | local calls | external calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| simple | 12 | 3/12 | 3/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 9 | 48 | 0 |
| all | 12 | 3/12 | 3/12 | 12/12 | 12/12 | - | - | - | 0 | 0 | 9 | 48 | 0 |

## Section 12.3 targets (English simple subset)

- auto: local_completion 4/12 (target 80%, not met); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- external_only: local_completion 0/12 (target 80%, not met); tool_correctness 12/12 (target 95%, met); missed confirmations 0 (met)
- local_only: local_completion 4/12 (target 80%, not met); tool_correctness 11/12 (target 95%, not met); missed confirmations 0 (met)
