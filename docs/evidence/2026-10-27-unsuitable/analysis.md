# Unsuitable detection on the phase 2 evidence (#170, spec 0.48)

Stage rule: the latest 3 local stages all ran out of local time, the latest within placement.probe_after_seconds. Index SHA-256 `a24dd1def6d46f7fbccb28eaa52b8ae7284bf25b05ee4264ec0da042c75dc224`.

## Replay per configuration

| configuration | cells | cells where the stage rule fires | longest streak | startup classes (cases) |
| --- | --- | --- | --- | --- |
| constrained | 7 | 7 | 26 | ready 1085, unsuitable 7 |
| normal | 8 | 0 | 2 | ready 1248 |

## Replay per cell

| cell | stages | ran out of time (by proxy) | longest streak | auto runs | auto runs classified unsuitable | first at acceptance | startup classes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| calibration-constrained-local_first-router-buffered-corpus | 100 | 55 (32) | 11 | 52 | 12 | 9 | ready 155, unsuitable 1 |
| calibration-normal-local_first-router-buffered-corpus | 100 | 0 (0) | 0 | 52 | 0 | none | ready 156 |
| placement-constrained-device_aware-example-router-buffered-corpus | 96 | 56 (0) | 11 | 52 | 11 | 9 | ready 155, unsuitable 1 |
| placement-constrained-device_aware-router-buffered-corpus | 100 | 61 (0) | 22 | 52 | 21 | 9 | ready 155, unsuitable 1 |
| placement-constrained-expected_wait-example-router-buffered-corpus | 79 | 47 (0) | 14 | 52 | 24 | 9 | ready 155, unsuitable 1 |
| placement-constrained-expected_wait-router-buffered-corpus | 102 | 66 (0) | 26 | 52 | 24 | 9 | ready 155, unsuitable 1 |
| placement-constrained-local_first-example-router-buffered-corpus | 97 | 50 (35) | 7 | 52 | 4 | 36 | ready 155, unsuitable 1 |
| placement-constrained-local_first-router-buffered-corpus | 101 | 55 (31) | 12 | 52 | 13 | 9 | ready 155, unsuitable 1 |
| placement-normal-device_aware-example-router-buffered-corpus | 92 | 5 (0) | 1 | 52 | 0 | none | ready 156 |
| placement-normal-device_aware-router-buffered-corpus | 100 | 2 (0) | 1 | 52 | 0 | none | ready 156 |
| placement-normal-expected_wait-example-router-buffered-corpus | 80 | 6 (0) | 1 | 52 | 0 | none | ready 156 |
| placement-normal-expected_wait-router-buffered-corpus | 101 | 15 (0) | 2 | 52 | 0 | none | ready 156 |
| placement-normal-local_first-example-router-buffered-corpus | 92 | 0 (0) | 0 | 52 | 0 | none | ready 156 |
| placement-normal-local_first-router-buffered-corpus | 100 | 0 (0) | 0 | 52 | 0 | none | ready 156 |
| streaming-normal-local_first-router-live-corpus | 100 | 2 (1) | 1 | 52 | 0 | none | ready 156 |

## Section 12.3 unsuitable hypothesis (section 14.4.7)

Status: **not met**. constrained local_first auto (example-router chain, classified unsuitable by the spec 0.48 stage rule) against none-class auto (example chain), paired by case and latency schedule; equal observed task success, lower p95 first useful content and completion, and no more timeouts, errors or unobserved requests in the candidate.

- Control: `placement-constrained-local_first-example-router-buffered`; candidate: `streaming-none-local_first-example-buffered`.
- Auto corpus pairs: N 52, success 45/52 (86.5%) (control) vs 43/52 (82.7%) (candidate), gains 3, losses 5.
- completion_ms: p95 53424.14 vs 55568.915 ms, p50 11794.437 vs 17604.998 ms, n 81 vs 68; lower p95: False; p50 agrees: True
- first_useful_content_ms: p95 53087.0 vs 54260.0 ms, p50 10358.0 vs 15827.0 ms, n 111 vs 98; lower p95: False; p50 agrees: True
- Latency reliability: control 0 timeouts, 9 errors, 0 unobserved; candidate 6, 16, 6; candidate worse: ['timeouts', 'errors', 'unobserved_total']

External work per latency request (calls, median call ms):

- candidate: clarification 0.0 calls, no call to time; routing 4.53 calls, 7833 ms; simple 2.27 calls, 8099 ms
- control: clarification 0.0 calls, no call to time; routing 4.23 calls, 7267 ms; simple 0.83 calls, 7392 ms

Limitations:

- The arms ran at different times on 25 and 26 October 2026; provider latency may have drifted between them.
- Startup warm-up time is in neither arm.
- The control arm is the recorded local_first run; with the spec 0.48 rule live, its auto runs after the first streak would have been managed external execution.
