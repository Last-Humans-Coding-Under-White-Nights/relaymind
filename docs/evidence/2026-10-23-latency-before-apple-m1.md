# Latency benchmark: lat-base

## all: 120 samples, 0 timeouts (fraction 0.000), 50 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 120 | 583 | 2491 |
| first_model_action_ms | 90 | 423 | 479 |
| first_useful_content_ms | 40 | 581 | 2236 |
| first_answer_token_ms | 40 | 583 | 2244 |
| completion_ms | 120 | 584 | 2493 |

## clarification: 30 samples, 0 timeouts (fraction 0.000), 10 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 411 | 476 |
| first_model_action_ms | 20 | 418 | 473 |
| first_useful_content_ms | 20 | 418 | 474 |
| first_answer_token_ms | 20 | 420 | 477 |
| completion_ms | 30 | 412 | 477 |

## routing: 30 samples, 0 timeouts (fraction 0.000), 0 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 421 | 1049 |
| first_model_action_ms | 10 | 409 | 466 |
| first_useful_content_ms | 0 | - | - |
| first_answer_token_ms | 0 | - | - |
| completion_ms | 30 | 421 | 1049 |

## simple: 60 samples, 0 timeouts (fraction 0.000), 40 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 60 | 1990 | 2603 |
| first_model_action_ms | 60 | 425 | 479 |
| first_useful_content_ms | 20 | 2023 | 2402 |
| first_answer_token_ms | 20 | 2027 | 2409 |
| completion_ms | 60 | 1991 | 2603 |

Hypothesis: p95 first_model_action_ms <= 500 ms: not met (p95 479 ms over 90 values, 120 samples, 0 timeouts).
