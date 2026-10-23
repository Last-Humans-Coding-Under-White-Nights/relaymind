# Latency benchmark: 2026-10-23-latency-windows-x64

## all: 120 samples, 0 timeouts (fraction 0.000), 76 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 120 | 4000 | 4032 |
| first_model_action_ms | 66 | 2000 | 3610 |
| first_useful_content_ms | 14 | 1891 | 3640 |
| first_answer_token_ms | 14 | 1907 | 3656 |
| completion_ms | 120 | 4000 | 4032 |

## clarification: 30 samples, 0 timeouts (fraction 0.000), 16 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 1969 | 4031 |
| first_model_action_ms | 14 | 1891 | 3640 |
| first_useful_content_ms | 14 | 1891 | 3640 |
| first_answer_token_ms | 14 | 1907 | 3656 |
| completion_ms | 30 | 1985 | 4031 |

## routing: 30 samples, 0 timeouts (fraction 0.000), 0 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 30 | 3375 | 4031 |
| first_model_action_ms | 6 | 2062 | 2172 |
| first_useful_content_ms | 0 | - | - |
| first_answer_token_ms | 0 | - | - |
| completion_ms | 30 | 3375 | 4031 |

## simple: 60 samples, 0 timeouts (fraction 0.000), 60 other errors

| metric | n | p50 ms | p95 ms |
|---|---|---|---|
| first_transport_ms | 60 | 4015 | 4032 |
| first_model_action_ms | 46 | 2047 | 3610 |
| first_useful_content_ms | 0 | - | - |
| first_answer_token_ms | 0 | - | - |
| completion_ms | 60 | 4015 | 4032 |

Hypothesis: p95 first_model_action_ms <= 500 ms: not met (p95 3610 ms over 66 values, 120 samples, 0 timeouts).
