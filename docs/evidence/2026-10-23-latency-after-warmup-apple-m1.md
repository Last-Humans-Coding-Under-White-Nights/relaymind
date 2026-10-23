# Latency benchmark: lat-after

## all: 120 samples, 0 timeouts (fraction 0.000), 50 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 120 | 704 | 2618 |
| first_model_action_ms | 90 | 438 | 583 |
| first_useful_content_ms | 40 | 609 | 2392 |
| first_answer_token_ms | 40 | 622 | 2405 |
| completion_ms | 120 | 705 | 2619 |

## clarification: 30 samples, 0 timeouts (fraction 0.000), 10 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 435 | 614 |
| first_model_action_ms | 20 | 443 | 601 |
| first_useful_content_ms | 20 | 443 | 602 |
| first_answer_token_ms | 20 | 446 | 615 |
| completion_ms | 30 | 435 | 617 |

## routing: 30 samples, 0 timeouts (fraction 0.000), 0 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 437 | 1073 |
| first_model_action_ms | 10 | 426 | 466 |
| first_useful_content_ms | 0 | - | - |
| first_answer_token_ms | 0 | - | - |
| completion_ms | 30 | 438 | 1075 |

## simple: 60 samples, 0 timeouts (fraction 0.000), 40 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 60 | 2041 | 2729 |
| first_model_action_ms | 60 | 438 | 557 |
| first_useful_content_ms | 20 | 2044 | 2708 |
| first_answer_token_ms | 20 | 2049 | 2737 |
| completion_ms | 60 | 2042 | 2730 |

Hypothesis: p95 first_model_action_ms <= 500 ms: not met (p95 583 ms over 90 values, 120 samples, 0 timeouts).
