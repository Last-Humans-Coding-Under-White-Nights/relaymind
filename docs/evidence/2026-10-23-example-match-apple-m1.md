# Example matcher on the corpus prompts: offline analysis of recorded traces, not a measured run

A throwaway BM25 matcher over the demo skills' example requests and descriptions, scored on the corpus prompts against the skill sidecar. Nothing here is a measured run of the selector in the gateway.

Masked identifiers: payments-api, staging-api. BM25 k1 1.5, b 0.75. Tokens: word tokens for Latin text, character bigrams for Chinese text.

## Best skill of every prompt, no thresholds

### Matcher

| group | cases | picks | right | wrong | none | macro precision | macro recall | macro Jaccard | micro precision | micro recall | micro Jaccard |
|---|---|---|---|---|---|---|---|---|---|---|---|
| en simple | 12 | 12 | 9 | 3 | 0 | 0.750 | 0.750 | 0.750 | 0.750 | 0.750 | 0.600 |
| en complex | 10 | 10 | 6 | 4 | 0 | 0.600 | 0.600 | 0.600 | 0.600 | 0.600 | 0.429 |
| en ambiguous | 6 | 6 | 5 | 1 | 0 | 0.833 | 0.833 | 0.833 | 0.833 | 0.833 | 0.714 |
| en mutation | 6 | 6 | 6 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| en adversarial | 6 | 6 | 3 | 3 | 0 | 0.500 | 0.500 | 0.500 | 0.500 | 0.500 | 0.333 |
| zh simple | 12 | 12 | 12 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| en all | 40 | 40 | 29 | 11 | 0 | 0.725 | 0.725 | 0.725 | 0.725 | 0.725 | 0.569 |
| zh all | 12 | 12 | 12 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

### Score and margin of the picks

| language | picks | count | score min | p25 | median | p75 | max | margin min | p25 | median | p75 | max |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| en | right | 29 | 2.111 | 4.611 | 5.777 | 8.608 | 11.663 | 0.127 | 1.821 | 4.734 | 7.495 | 9.972 |
| en | wrong | 11 | 1.909 | 2.623 | 3.785 | 4.387 | 5.601 | 0.015 | 0.076 | 0.245 | 1.296 | 2.064 |
| zh | right | 12 | 1.562 | 3.318 | 7.826 | 10.028 | 14.596 | 0.162 | 0.978 | 7.272 | 8.738 | 13.196 |
| zh | wrong | 0 | - | - | - | - | - | - | - | - | - | - |

## Local router in the export (the baseline)

The auto and local_only runs picked different skills for 0 of 52 cases that have both.

### Router, auto

| group | cases | picks | right | wrong | none | macro precision | macro recall | macro Jaccard | micro precision | micro recall | micro Jaccard |
|---|---|---|---|---|---|---|---|---|---|---|---|
| en simple | 12 | 12 | 11 | 1 | 0 | 0.917 | 0.917 | 0.917 | 0.917 | 0.917 | 0.846 |
| en complex | 10 | 9 | 8 | 1 | 1 | 0.800 | 0.800 | 0.800 | 0.889 | 0.800 | 0.727 |
| en ambiguous | 6 | 6 | 5 | 1 | 0 | 0.833 | 0.833 | 0.833 | 0.833 | 0.833 | 0.714 |
| en mutation | 6 | 2 | 2 | 0 | 4 | 0.333 | 0.333 | 0.333 | 1.000 | 0.333 | 0.333 |
| en adversarial | 6 | 5 | 4 | 1 | 1 | 0.667 | 0.667 | 0.667 | 0.800 | 0.667 | 0.571 |
| zh simple | 12 | 12 | 12 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| en all | 40 | 34 | 30 | 4 | 6 | 0.750 | 0.750 | 0.750 | 0.882 | 0.750 | 0.682 |
| zh all | 12 | 12 | 12 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

### Router, local_only

| group | cases | picks | right | wrong | none | macro precision | macro recall | macro Jaccard | micro precision | micro recall | micro Jaccard |
|---|---|---|---|---|---|---|---|---|---|---|---|
| en simple | 12 | 12 | 11 | 1 | 0 | 0.917 | 0.917 | 0.917 | 0.917 | 0.917 | 0.846 |
| en complex | 10 | 9 | 8 | 1 | 1 | 0.800 | 0.800 | 0.800 | 0.889 | 0.800 | 0.727 |
| en ambiguous | 6 | 6 | 5 | 1 | 0 | 0.833 | 0.833 | 0.833 | 0.833 | 0.833 | 0.714 |
| en mutation | 6 | 2 | 2 | 0 | 4 | 0.333 | 0.333 | 0.333 | 1.000 | 0.333 | 0.333 |
| en adversarial | 6 | 5 | 4 | 1 | 1 | 0.667 | 0.667 | 0.667 | 0.800 | 0.667 | 0.571 |
| zh simple | 12 | 12 | 12 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| en all | 40 | 34 | 30 | 4 | 6 | 0.750 | 0.750 | 0.750 | 0.882 | 0.750 | 0.682 |
| zh all | 12 | 12 | 12 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

## Candidate thresholds

Rule: the thresholds with the most right English picks among those whose English wrong picks are at or below the router's (the lowest of its samples). Router English wrong picks: auto 4, local_only 4; limit 4. Thresholds: score at least 2.8164, margin at least 0.1268. Chosen on the cases that score them; not held out.

### English at the thresholds

| group | cases | picks | right | wrong | none | macro precision | macro recall | macro Jaccard | micro precision | micro recall | micro Jaccard |
|---|---|---|---|---|---|---|---|---|---|---|---|
| en simple | 12 | 10 | 9 | 1 | 2 | 0.750 | 0.750 | 0.750 | 0.900 | 0.750 | 0.692 |
| en complex | 10 | 7 | 5 | 2 | 3 | 0.500 | 0.500 | 0.500 | 0.714 | 0.500 | 0.417 |
| en ambiguous | 6 | 6 | 5 | 1 | 0 | 0.833 | 0.833 | 0.833 | 0.833 | 0.833 | 0.714 |
| en mutation | 6 | 6 | 6 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| en adversarial | 6 | 2 | 2 | 0 | 4 | 0.333 | 0.333 | 0.333 | 1.000 | 0.333 | 0.333 |
| en all | 40 | 31 | 27 | 4 | 9 | 0.675 | 0.675 | 0.675 | 0.871 | 0.675 | 0.614 |

### Chinese at the thresholds

| group | cases | picks | right | wrong | none | macro precision | macro recall | macro Jaccard | micro precision | micro recall | micro Jaccard |
|---|---|---|---|---|---|---|---|---|---|---|---|
| zh simple | 12 | 11 | 11 | 0 | 1 | 0.917 | 0.917 | 0.917 | 1.000 | 0.917 | 0.917 |
| zh all | 12 | 11 | 11 | 0 | 1 | 0.917 | 0.917 | 0.917 | 1.000 | 0.917 | 0.917 |

### Right and wrong English picks along the frontier

| wrong picks | right picks | score at least | margin at least |
|---|---|---|---|
| 0 | 19 | 4.9207 | 2.2011 |
| 1 | 22 | 4.0387 | 1.8205 |
| 2 | 25 | 2.8164 | 1.3343 |
| 3 | 26 | 2.8164 | 0.4985 |
| 4 | 27 | 2.8164 | 0.1268 |
| 5 | 28 | 2.1106 | 0.2847 |
| 6 | 29 | 2.1106 | 0.1268 |

## Caveats

- Each skill has only a few example pairs (check-service-health 2, delete-demo-database 1, health-and-diagnostic-checklist 1, investigate-performance 2, lookup-latest-deployment 2, summarize-incident-counts 2), so the index is tiny and its word statistics are coarse.
- The thresholds are chosen on the same 52 cases that score them; the result is optimistic and is not held out.
- The router figures are the two runs per case in one export (auto and local_only, one machine). The gateway ran the router and the local skill, repair, and final calls at temperature 0, so the two runs are repeats of a near-deterministic call, not independent samples; a second run could still differ, because the runtime does not promise identical output across runs. They are not a distribution.
- Every corpus case expects one skill and a selector picks at most one, so the macro precision, recall, and Jaccard are equal on the corpus; the micro averages differ. The ops corpus cannot tell a good selector from a poor one, which is why section 12.4 adds the selection benchmarks.
- Chinese cases are best-effort versions of the simple English cases and have not been natively reviewed; they are reported apart.
