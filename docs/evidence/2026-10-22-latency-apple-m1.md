# Latency benchmark: 2026-10-22-latency-apple-m1

## all: 120 samples, 0 timeouts (fraction 0.000), 50 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 120 | 552 | 2517 |
| first_model_action_ms | 90 | 442 | 508 |
| first_useful_content_ms | 40 | 549 | 2328 |
| first_answer_token_ms | 40 | 553 | 2345 |
| completion_ms | 120 | 553 | 2517 |

## clarification: 30 samples, 0 timeouts (fraction 0.000), 10 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 429 | 542 |
| first_model_action_ms | 20 | 439 | 536 |
| first_useful_content_ms | 20 | 439 | 536 |
| first_answer_token_ms | 20 | 442 | 543 |
| completion_ms | 30 | 429 | 543 |

## routing: 30 samples, 0 timeouts (fraction 0.000), 0 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 440 | 1083 |
| first_model_action_ms | 10 | 437 | 494 |
| first_useful_content_ms | 0 | - | - |
| first_answer_token_ms | 0 | - | - |
| completion_ms | 30 | 440 | 1084 |

## simple: 60 samples, 0 timeouts (fraction 0.000), 40 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 60 | 2068 | 2606 |
| first_model_action_ms | 60 | 442 | 505 |
| first_useful_content_ms | 20 | 1953 | 2465 |
| first_answer_token_ms | 20 | 1957 | 2474 |
| completion_ms | 60 | 2068 | 2607 |

Hypothesis: p95 first_model_action_ms <= 500 ms: not met (p95 508 ms over 90 values, 120 samples, 0 timeouts).
