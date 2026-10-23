# Draft answer pass: offline counterfactual, not a measured run

The stored traces of a measured corpus run, graded as if the skill-stage draft had been published as the answer (RM31). No model was called and the source export is not changed. A blocked run keeps its measured grade. This is not a measured run: the draft pass was never run, so the figures are a prediction of it.

Source export: corpus digest `83023ab1ae1d29508caf367d28a4adbb0a4e4d93e977ea4628d94823ecd5d3bd`, application `c3aa77aa79d9bf1213ccc4c974fc8de010641ece`.

## Drafts published and blocked

Runs of auto and local_only in which a final generation ran. A measured run that did not complete locally is the case of finding F1; one that completed locally is checked so that the draft does not lower success.

| mode | language | class | measured run | runs | published | published and passing | blocked | reasons | not gradable | measured passing | gained | lost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| auto | en | simple | not local | 2 | 2 | 2/2 | 0 | - | 0 | 2/2 | 0 | 0 |
| auto | en | simple | completed locally | 5 | 5 | 4/5 | 0 | - | 0 | 4/5 | 0 | 0 |
| auto | en | complex | completed locally | 1 | 1 | 0/1 | 0 | - | 0 | 0/1 | 0 | 0 |
| auto | en | ambiguous | not local | 1 | 1 | 1/1 | 0 | - | 0 | 1/1 | 0 | 0 |
| auto | en | ambiguous | completed locally | 2 | 2 | 2/2 | 0 | - | 0 | 1/2 | 1 | 0 |
| auto | zh | simple | not local | 3 | 1 | 1/1 | 2 | language 2 | 0 | 3/3 | 0 | 0 |
| auto | zh | simple | completed locally | 3 | 1 | 1/1 | 2 | language 2 | 0 | 2/3 | 0 | 0 |
| local_only | en | simple | not local | 3 | 3 | 3/3 | 0 | - | 0 | 0/3 | 3 | 0 |
| local_only | en | simple | completed locally | 5 | 5 | 4/5 | 0 | - | 0 | 4/5 | 0 | 0 |
| local_only | en | complex | completed locally | 1 | 1 | 0/1 | 0 | - | 0 | 0/1 | 0 | 0 |
| local_only | en | ambiguous | not local | 1 | 1 | 1/1 | 0 | - | 0 | 0/1 | 1 | 0 |
| local_only | en | ambiguous | completed locally | 2 | 2 | 2/2 | 0 | - | 0 | 2/2 | 0 | 0 |
| local_only | zh | simple | not local | 4 | 1 | 1/1 | 3 | language 3 | 0 | 0/4 | 1 | 0 |
| local_only | zh | simple | completed locally | 3 | 1 | 1/1 | 2 | language 2 | 0 | 3/3 | 0 | 0 |

## Task success and local completion: measured and counterfactual

| mode | language | class | success measured | success counterfactual | local completion measured | local completion counterfactual | local calls | external calls |
|---|---|---|---|---|---|---|---|---|
| auto | en | simple | 11/12 | 11/12 | 4/12 | 6/12 | 45 to 38 | 14 to 11 |
| auto | en | complex | 2/10 | 2/10 | - | - | 13 to 12 | 46 to 46 |
| auto | en | ambiguous | 4/6 | 5/6 | - | - | 21 to 18 | 9 to 8 |
| auto | en | mutation | 3/6 | 3/6 | - | - | 12 to 12 | 14 to 14 |
| auto | en | adversarial | 4/6 | 4/6 | - | - | 11 to 11 | 16 to 16 |
| auto | en | all | 24/40 | 25/40 | 4/12 | 6/12 | 102 to 91 | 99 to 95 |
| auto | zh | simple | 11/12 | 11/12 | 2/12 | 3/12 | 48 to 46 | 16 to 15 |
| auto | zh | all | 11/12 | 11/12 | 2/12 | 3/12 | 48 to 46 | 16 to 15 |
| local_only | en | simple | 4/12 | 7/12 | 4/12 | 7/12 | 45 to 37 | 0 to 0 |
| local_only | en | complex | 0/10 | 0/10 | - | - | 13 to 12 | 0 to 0 |
| local_only | en | ambiguous | 2/6 | 3/6 | - | - | 21 to 18 | 0 to 0 |
| local_only | en | mutation | 0/6 | 0/6 | - | - | 12 to 12 | 0 to 0 |
| local_only | en | adversarial | 4/6 | 4/6 | - | - | 11 to 11 | 0 to 0 |
| local_only | en | all | 10/40 | 14/40 | 4/12 | 7/12 | 102 to 90 | 0 to 0 |
| local_only | zh | simple | 3/12 | 4/12 | 3/12 | 4/12 | 48 to 46 | 0 to 0 |
| local_only | zh | all | 3/12 | 4/12 | 3/12 | 4/12 | 48 to 46 | 0 to 0 |
