# Several read-only calls per generation: offline analysis of recorded traces, not a measured run

Generations that proposed two or more calls, graded against the section 4.3 rule for calls_per_generation many. Saved generations are a prediction from recorded behavior, not a measurement.

| mode | language | class | runs | model calls in these runs | generations | proposals | admitted | kept rejected | mixed | saved generations | re-proposed with other arguments |
|---|---|---|---|---|---|---|---|---|---|---|---|
| auto | en | complex | 7 | 49 | 7 | 22 | 7 | - | 0 | 19 | 3 |
| external_only | en | complex | 10 | 47 | 10 | 43 | 10 | - | 0 | 23 | 4 |
| external_only | en | ambiguous | 4 | 10 | 4 | 8 | 0 | invalid_call 4 | 0 | 0 | 0 |
| all en | | | | | 21 | 73 | 17 | invalid_call 4 | 0 | 42 | 7 |

Generations by actor: external 21.

Cross-check: 21 generations with 73 proposals; the trace records 73 proposals refused with multiple_tool_calls (agree).
