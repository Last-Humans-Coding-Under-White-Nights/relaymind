# M8 comparison

Offline analysis of finalized exports. Single pass per cell; provider drift and sampling uncertainty remain. No default changes follow from this report.

## Provenance

- Measured revision: 741a19175430e83702a360c454a581b2829e3cb0
- Matrix root (actual): /private/tmp/relaymind-phase2-measurements-2026-10-25/m8
- Matrix root (recorded): /private/tmp/relaymind-phase2-measurements-2026-10-25/m8
- Measurement checkout (recorded): /private/tmp/relaymind-m7-completion
- plan.json sha256: cf5c5bd202272c39afdf0bb8e835df1a01e18a9787b9547a3229a15d435814ee
- index.json sha256: a24dd1def6d46f7fbccb28eaa52b8ae7284bf25b05ee4264ec0da042c75dc224
- Validator files differing from the measured revision: scripts/run_corpus.py, scripts/run_m8_matrix.py

## Inventory

31 of 31 cells complete; 0 incomplete, 0 missing, 0 invalid.

| cell | status | attempts | export sha256 | reason |
|---|---|---|---|---|
| calibration-normal-local_first-router-buffered-corpus | complete | initial:complete | 0d0f5cd5fb51 |  |
| calibration-constrained-local_first-router-buffered-corpus | complete | initial:complete | e2d5099b3e23 |  |
| placement-normal-local_first-router-buffered-corpus | complete | initial:complete | ba3f4fc59fd4 |  |
| placement-normal-local_first-router-buffered-latency | complete | initial:complete | 90851a3bc5fb |  |
| placement-normal-local_first-example-router-buffered-corpus | complete | initial:complete | 6cc3ad3112c8 |  |
| placement-normal-local_first-example-router-buffered-latency | complete | initial:complete | 1194e895cd6d |  |
| placement-normal-device_aware-router-buffered-corpus | complete | initial:complete | dc2d6bdf9c87 |  |
| placement-normal-device_aware-router-buffered-latency | complete | initial:complete | 63f40fbfff61 |  |
| placement-normal-device_aware-example-router-buffered-corpus | complete | initial:complete | dc443fa758b7 |  |
| placement-normal-device_aware-example-router-buffered-latency | complete | initial:complete | fd9cfef643f1 |  |
| placement-normal-expected_wait-router-buffered-corpus | complete | initial:complete | a027958d49dd |  |
| placement-normal-expected_wait-router-buffered-latency | complete | initial:complete | aad5fee8e943 |  |
| placement-normal-expected_wait-example-router-buffered-corpus | complete | initial:complete | 8258bf7dab1c |  |
| placement-normal-expected_wait-example-router-buffered-latency | complete | initial:complete | e84eadff4282 |  |
| placement-constrained-local_first-router-buffered-corpus | complete | initial:complete | fb80bc1138c1 |  |
| placement-constrained-local_first-router-buffered-latency | complete | initial:complete | 7a00d8d7bed6 |  |
| placement-constrained-local_first-example-router-buffered-corpus | complete | initial:complete | b26c9daeb767 |  |
| placement-constrained-local_first-example-router-buffered-latency | complete | initial:complete | 0efde0e64e99 |  |
| placement-constrained-device_aware-router-buffered-corpus | complete | initial:complete | 4a15c5b2646b |  |
| placement-constrained-device_aware-router-buffered-latency | complete | initial:complete | 209c1d1b2b81 |  |
| placement-constrained-device_aware-example-router-buffered-corpus | complete | initial:complete | 3afcd3e9152e |  |
| placement-constrained-device_aware-example-router-buffered-latency | complete | initial:complete | 9349f0bc60da |  |
| placement-constrained-expected_wait-router-buffered-corpus | complete | initial:complete | 1a02c97015b7 |  |
| placement-constrained-expected_wait-router-buffered-latency | complete | initial:complete | 01e9a52e87c8 |  |
| placement-constrained-expected_wait-example-router-buffered-corpus | complete | initial:interrupted, resume-1:complete | 503e4cd14223 |  |
| placement-constrained-expected_wait-example-router-buffered-latency | complete | initial:complete | 3a89106921f5 |  |
| streaming-normal-local_first-router-live-corpus | complete | initial:complete | 212a5fce094c |  |
| streaming-none-local_first-example-buffered-corpus | complete | initial:complete | 561b23178be8 |  |
| streaming-none-local_first-example-buffered-latency | complete | initial:complete | 3e858798dfe7 |  |
| streaming-none-local_first-example-live-corpus | complete | initial:complete | 5b0f9737a21a |  |
| streaming-none-local_first-example-live-latency | complete | initial:complete | 650e85dfb056 |  |

## Corpus comparisons

| question | configuration | control | candidate | success A | success B | gain | loss | unchanged success/failure | delta | parity | class transitions |
|---|---|---|---|---|---|---|---|---|---|---|---|
| device_aware_placement | normal | placement-normal-local_first-router-buffered-corpus | placement-normal-device_aware-router-buffered-corpus | 88/156 | 83/156 | 1 | 6 | 82/67 | -0.032 | parity not met | {'ready/ready': 156} |
| expected_wait_vs_baseline | normal | placement-normal-local_first-router-buffered-corpus | placement-normal-expected_wait-router-buffered-corpus | 88/156 | 84/156 | 3 | 7 | 81/65 | -0.026 | parity not met | {'ready/ready': 156} |
| expected_wait_rule | normal | placement-normal-device_aware-router-buffered-corpus | placement-normal-expected_wait-router-buffered-corpus | 83/156 | 84/156 | 5 | 4 | 79/68 | 0.006 | improved observed task success | {'ready/ready': 156} |
| device_aware_placement | normal | placement-normal-local_first-example-router-buffered-corpus | placement-normal-device_aware-example-router-buffered-corpus | 88/156 | 87/156 | 4 | 5 | 83/64 | -0.006 | parity not met | {'ready/ready': 156} |
| expected_wait_vs_baseline | normal | placement-normal-local_first-example-router-buffered-corpus | placement-normal-expected_wait-example-router-buffered-corpus | 88/156 | 89/156 | 7 | 6 | 82/61 | 0.006 | improved observed task success | {'ready/ready': 156} |
| expected_wait_rule | normal | placement-normal-device_aware-example-router-buffered-corpus | placement-normal-expected_wait-example-router-buffered-corpus | 87/156 | 89/156 | 8 | 6 | 81/61 | 0.013 | improved observed task success | {'ready/ready': 156} |
| example_first_chain | normal | placement-normal-local_first-router-buffered-corpus | placement-normal-local_first-example-router-buffered-corpus | 88/156 | 88/156 | 6 | 6 | 82/62 | 0.000 | equal observed task success | {'ready/ready': 156} |
| example_first_chain | normal | placement-normal-device_aware-router-buffered-corpus | placement-normal-device_aware-example-router-buffered-corpus | 83/156 | 87/156 | 6 | 2 | 81/67 | 0.026 | improved observed task success | {'ready/ready': 156} |
| example_first_chain | normal | placement-normal-expected_wait-router-buffered-corpus | placement-normal-expected_wait-example-router-buffered-corpus | 84/156 | 89/156 | 8 | 3 | 81/64 | 0.032 | improved observed task success | {'ready/ready': 156} |
| device_aware_placement | constrained | placement-constrained-local_first-router-buffered-corpus | placement-constrained-device_aware-router-buffered-corpus | 83/156 | 80/156 | 1 | 4 | 79/72 | -0.019 | parity not met | {'ready/ready': 156} |
| expected_wait_vs_baseline | constrained | placement-constrained-local_first-router-buffered-corpus | placement-constrained-expected_wait-router-buffered-corpus | 83/156 | 82/156 | 5 | 6 | 77/68 | -0.006 | parity not met | {'ready/ready': 156} |
| expected_wait_rule | constrained | placement-constrained-device_aware-router-buffered-corpus | placement-constrained-expected_wait-router-buffered-corpus | 80/156 | 82/156 | 5 | 3 | 77/71 | 0.013 | improved observed task success | {'ready/ready': 156} |
| device_aware_placement | constrained | placement-constrained-local_first-example-router-buffered-corpus | placement-constrained-device_aware-example-router-buffered-corpus | 89/156 | 87/156 | 2 | 4 | 85/65 | -0.013 | parity not met | {'ready/ready': 156} |
| expected_wait_vs_baseline | constrained | placement-constrained-local_first-example-router-buffered-corpus | placement-constrained-expected_wait-example-router-buffered-corpus | 89/156 | 79/156 | 1 | 11 | 78/66 | -0.064 | parity not met | {'ready/ready': 156} |
| expected_wait_rule | constrained | placement-constrained-device_aware-example-router-buffered-corpus | placement-constrained-expected_wait-example-router-buffered-corpus | 87/156 | 79/156 | 3 | 11 | 76/66 | -0.051 | parity not met | {'ready/ready': 156} |
| example_first_chain | constrained | placement-constrained-local_first-router-buffered-corpus | placement-constrained-local_first-example-router-buffered-corpus | 83/156 | 89/156 | 8 | 2 | 81/65 | 0.038 | improved observed task success | {'ready/ready': 156} |
| example_first_chain | constrained | placement-constrained-device_aware-router-buffered-corpus | placement-constrained-device_aware-example-router-buffered-corpus | 80/156 | 87/156 | 8 | 1 | 79/68 | 0.045 | improved observed task success | {'ready/ready': 156} |
| example_first_chain | constrained | placement-constrained-expected_wait-router-buffered-corpus | placement-constrained-expected_wait-example-router-buffered-corpus | 82/156 | 79/156 | 5 | 8 | 74/69 | -0.019 | parity not met | {'ready/ready': 156} |
| local_live_streaming | normal | placement-normal-local_first-router-buffered-corpus | streaming-normal-local_first-router-live-corpus | 88/156 | 74/156 | 2 | 16 | 72/66 | -0.090 | parity not met | {'ready/ready': 156} |
| none_live_streaming | none | streaming-none-local_first-example-buffered-corpus | streaming-none-local_first-example-live-corpus | 77/104 | 73/104 | 2 | 6 | 71/25 | -0.038 | parity not met | {'absent/absent': 104} |

## Latency comparisons

Each arm's full observed population; completion counts successful final answers only.

| question | configuration | control | candidate | first useful p50/p95 A (n) | B (n) | completion p95 A (n) | B (n) | errors/timeouts/unobserved A | B |
|---|---|---|---|---|---|---|---|---|---|
| device_aware_placement | normal | placement-normal-local_first-router-buffered-latency | placement-normal-device_aware-router-buffered-latency | 8947/22241 (91) | 9005/21945 (89) | 23583 (71) | 22295 (69) | 29/0/0 | 31/0/0 |
| expected_wait_vs_baseline | normal | placement-normal-local_first-router-buffered-latency | placement-normal-expected_wait-router-buffered-latency | 8947/22241 (91) | 9771/25667 (91) | 23583 (71) | 25895 (71) | 29/0/0 | 29/0/0 |
| expected_wait_rule | normal | placement-normal-device_aware-router-buffered-latency | placement-normal-expected_wait-router-buffered-latency | 9005/21945 (89) | 9771/25667 (91) | 22295 (69) | 25895 (71) | 31/0/0 | 29/0/0 |
| device_aware_placement | normal | placement-normal-local_first-example-router-buffered-latency | placement-normal-device_aware-example-router-buffered-latency | 1869/51338 (109) | 1862/48465 (110) | 51873 (79) | 48778 (80) | 10/1/1 | 10/0/0 |
| expected_wait_vs_baseline | normal | placement-normal-local_first-example-router-buffered-latency | placement-normal-expected_wait-example-router-buffered-latency | 1869/51338 (109) | 2020/48858 (110) | 51873 (79) | 49476 (80) | 10/1/1 | 10/0/0 |
| expected_wait_rule | normal | placement-normal-device_aware-example-router-buffered-latency | placement-normal-expected_wait-example-router-buffered-latency | 1862/48465 (110) | 2020/48858 (110) | 48778 (80) | 49476 (80) | 10/0/0 | 10/0/0 |
| example_first_chain | normal | placement-normal-local_first-router-buffered-latency | placement-normal-local_first-example-router-buffered-latency | 8947/22241 (91) | 1869/51338 (109) | 23583 (71) | 51873 (79) | 29/0/0 | 10/1/1 |
| example_first_chain | normal | placement-normal-device_aware-router-buffered-latency | placement-normal-device_aware-example-router-buffered-latency | 9005/21945 (89) | 1862/48465 (110) | 22295 (69) | 48778 (80) | 31/0/0 | 10/0/0 |
| example_first_chain | normal | placement-normal-expected_wait-router-buffered-latency | placement-normal-expected_wait-example-router-buffered-latency | 9771/25667 (91) | 2020/48858 (110) | 25895 (71) | 49476 (80) | 29/0/0 | 10/0/0 |
| device_aware_placement | constrained | placement-constrained-local_first-router-buffered-latency | placement-constrained-device_aware-router-buffered-latency | 11402/26364 (87) | 10962/23594 (89) | 28615 (70) | 24537 (69) | 33/0/0 | 31/0/0 |
| expected_wait_vs_baseline | constrained | placement-constrained-local_first-router-buffered-latency | placement-constrained-expected_wait-router-buffered-latency | 11402/26364 (87) | 13827/24328 (86) | 28615 (70) | 24629 (66) | 33/0/0 | 34/0/0 |
| expected_wait_rule | constrained | placement-constrained-device_aware-router-buffered-latency | placement-constrained-expected_wait-router-buffered-latency | 10962/23594 (89) | 13827/24328 (86) | 24537 (69) | 24629 (66) | 31/0/0 | 34/0/0 |
| device_aware_placement | constrained | placement-constrained-local_first-example-router-buffered-latency | placement-constrained-device_aware-example-router-buffered-latency | 10358/53087 (111) | 8088/48826 (110) | 53424 (81) | 49064 (80) | 9/0/0 | 10/0/0 |
| expected_wait_vs_baseline | constrained | placement-constrained-local_first-example-router-buffered-latency | placement-constrained-expected_wait-example-router-buffered-latency | 10358/53087 (111) | 14772/53479 (107) | 53424 (81) | 55840 (77) | 9/0/0 | 10/3/3 |
| expected_wait_rule | constrained | placement-constrained-device_aware-example-router-buffered-latency | placement-constrained-expected_wait-example-router-buffered-latency | 8088/48826 (110) | 14772/53479 (107) | 49064 (80) | 55840 (77) | 10/0/0 | 10/3/3 |
| example_first_chain | constrained | placement-constrained-local_first-router-buffered-latency | placement-constrained-local_first-example-router-buffered-latency | 11402/26364 (87) | 10358/53087 (111) | 28615 (70) | 53424 (81) | 33/0/0 | 9/0/0 |
| example_first_chain | constrained | placement-constrained-device_aware-router-buffered-latency | placement-constrained-device_aware-example-router-buffered-latency | 10962/23594 (89) | 8088/48826 (110) | 24537 (69) | 49064 (80) | 31/0/0 | 10/0/0 |
| example_first_chain | constrained | placement-constrained-expected_wait-router-buffered-latency | placement-constrained-expected_wait-example-router-buffered-latency | 13827/24328 (86) | 14772/53479 (107) | 24629 (66) | 55840 (77) | 34/0/0 | 10/3/3 |
| none_live_streaming | none | streaming-none-local_first-example-buffered-latency | streaming-none-local_first-example-live-latency | 15827/54260 (98) | 16980/49521 (97) | 55569 (68) | 56225 (63) | 16/6/6 | 19/8/6 |

## Streamed failures (RM32)

| comparison | arm | configured | effective | predicate_unmet / effective live | partial-stream diagnostic | terminal errors |
|---|---|---|---|---|---|---|
| streaming-normal-local_first-router-live-corpus | A | {'buffered': 156} | {'buffered': 156} | 0/0 | 0 | {'needs_external': 40, 'external_failed': 8} |
| streaming-normal-local_first-router-live-corpus | B | {'live': 156} | {'live': 156} | 14/156 | 17 | {'needs_external': 33, 'predicate_unmet': 14, 'external_failed': 14} |
| streaming-none-local_first-example-live-corpus | A | {'buffered': 104} | {'buffered': 104} | 0/0 | 0 | {'external_failed': 13} |
| streaming-none-local_first-example-live-corpus | B | {'live': 104} | {'live': 104} | 1/104 | 1 | {'external_failed': 16, 'predicate_unmet': 1} |

## Physical cost ledgers

| ledger | attempts | calls | input known/missing | output known/missing | calls per success | scope |
|---|---|---|---|---|---|---|
| calibration | 2 | 455 | 678956/9 | 56151/9 | 2.676470588235294 | calibration population |
| candidate_corpus | 16 | 3832 | 5512771/117 | 411620/117 | 3.082864038616251 | candidate corpus task successes |
| candidate_latency | 14 | 3242 | 5070673/27 | 274238/27 | 3.166015625 | latency successful final answers (endpoint outcome, not task success) |

## Expected wait applicability

| cell | evaluated | declined | not evaluated | prerequisite variants (rows) |
|---|---|---|---|---|
| placement-normal-expected_wait-router-buffered-corpus | 73 | 13 | 505 | 1 (6) |
| placement-normal-expected_wait-router-buffered-latency | 110 | 20 | 353 | 1 (6) |
| placement-normal-expected_wait-example-router-buffered-corpus | 73 | 17 | 520 | 1 (6) |
| placement-normal-expected_wait-example-router-buffered-latency | 140 | 20 | 310 | 1 (6) |
| placement-constrained-expected_wait-router-buffered-corpus | 45 | 22 | 517 | 1 (6) |
| placement-constrained-expected_wait-router-buffered-latency | 58 | 46 | 392 | 1 (6) |
| placement-constrained-expected_wait-example-router-buffered-corpus | 52 | 26 | 536 | 1 (6) |
| placement-constrained-expected_wait-example-router-buffered-latency | 72 | 51 | 356 | 1 (6) |

## Section 12.3 hypotheses and targets

| hypothesis | status | scope | required external evidence |
|---|---|---|---|
| english_simple_local_completion | not met | auto/en/simple, proven local final owner and zero external calls; target 80%; baseline local_first/router cells per configuration, never pooled; Chinese apart |  |
| english_simple_tool_proposals | met | correct observed model proposals over observed model proposals (auto/en/simple); the run-level tool_correct rubric is reported apart; n=0 is not measured |  |
| no_missed_confirmation | met | every corpus cell, mode and language; reached confirmations and started mutating tools (from the exported traces) expose a vacuous zero |  |
| warm_500ms_model_action | not met | p95 first model action <= 500 ms over all 120 measured requests, no timeouts; per configuration; example chains and the none class are excluded because deterministic example selection is not a measured model action |  |
| unsuitable_device_aware | not measured | matched-unsuitable auto corpus pairs at equal observed success, and each arm's unsuitable latency population with lower p95 first useful content and completion; not met when the candidate arm has more timeouts, errors or unobserved requests; scopes without an unsuitable cohort are not measured |  |
| example_matcher_jaccard | not measured | English auto initial-selection chain Jaccard, example-router chain versus router; a chain score does not prove standalone matcher accuracy | the section 12.4 SkillsBench comparison under the router's eight-skill cap, with its locked exports and revisions |
| none_live_streaming | not met | none-class corpus no-lower task success and lower p95 first useful content; not met when the live arm has more timeouts, errors or unobserved requests |  |
| local_live_streaming_parity | not met | RM32 #125 configured-arm parity on one revision and frozen prior; no statistical equivalence margin is specified |  |
| external_cost_next_to_success | met | observed external calls and per-model usage next to task success in every candidate corpus and latency cell; unknown usage stays explicit; no monetary claim without a dated rate source |  |
| draft_local_completion | not measured | Every M8 candidate uses answer_pass=final. | valid paired M7 C0/C1 corpus evidence (#128) |
| prefetch_and_many_generations | not measured | M8 keeps prefetch off and one call per generation. | M7 factor cells C0, C2 and C3 with the English complex population; no Chinese complex cohort exists |
| criteria_corrections | not measured | M8 has no R-minus-criteria arm; its corrections are diagnostics only. | the separately reviewed section 14.4.4 criteria ablation (M7-C6) |
| early_headers | not measured | early_headers=false in every M8 cell. | separate protocol or M7 L0/L1 evidence with a generation-start clock |
| tool_narrowing_precision | not measured | M8 skill-chain data cannot measure tool narrowing. | the section 12.4 ToolRet proposal and narrowing comparison on the locked files |

Defaults: Defaults stay unchanged. A subsequent evidence-backed specification revision makes the explicit keep or change decision (spec 14.4.3, #134).

Reporting acceptance: passed.
