# Latency benchmark: lat-after-nowarm

## all: 120 samples, 0 timeouts (fraction 0.000), 50 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 120 | 567 | 2691 |
| first_model_action_ms | 90 | 456 | 558 |
| first_useful_content_ms | 40 | 531 | 2506 |
| first_answer_token_ms | 40 | 537 | 2517 |
| completion_ms | 120 | 568 | 2693 |

## clarification: 30 samples, 0 timeouts (fraction 0.000), 10 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 441 | 536 |
| first_model_action_ms | 20 | 442 | 520 |
| first_useful_content_ms | 20 | 442 | 520 |
| first_answer_token_ms | 20 | 448 | 525 |
| completion_ms | 30 | 442 | 537 |

## routing: 30 samples, 0 timeouts (fraction 0.000), 0 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 437 | 1057 |
| first_model_action_ms | 10 | 428 | 536 |
| first_useful_content_ms | 0 | - | - |
| first_answer_token_ms | 0 | - | - |
| completion_ms | 30 | 438 | 1057 |

## simple: 60 samples, 0 timeouts (fraction 0.000), 40 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 60 | 2193 | 2876 |
| first_model_action_ms | 60 | 459 | 569 |
| first_useful_content_ms | 20 | 2222 | 2627 |
| first_answer_token_ms | 20 | 2225 | 2639 |
| completion_ms | 60 | 2194 | 2880 |

Hypothesis: p95 first_model_action_ms <= 500 ms: not met (p95 558 ms over 90 values, 120 samples, 0 timeouts).
