# Limitations of m10-owner-run-260-adaptation

RM16, RM40, RM42, RM44; AC31, AC33, AC36; spec section 12.6.6.
[The record](m10-owner-run-260-adaptation.json) remains pending and
non-qualifying. Its profile fields describe the intended procedure, not an
approved result. Its closed schema has no free-text limitations field; this
companion documents the defects without changing bound historical evidence.
Approval alone cannot repair these defects.

## Raw judgments and agreement

The original replies are in these files under
`evaluation/assets/owner-run-260/logs/`:

- A: `annotator-a/machine-annotator-a-012.response.json`.
- B: `annotator-b/machine-annotator-b-007.response.json`.

The call logs bind response hashes and lengths, and request hashes and
lengths. No original request bodies are committed. Thus response bytes can be
compared with `labels.json`, but exact request contents cannot be verified.

| Annotator | Case | Answered class / acceptable handle | Stored class / acceptable set |
| --- | --- | --- | --- |
| A | e016-c1 | positive / A54 | negative / empty |
| A | e016-p2 | positive / A54 | negative / empty |
| A | e094-c1 | positive / A52 | negative / empty |
| B | e055-c1 | positive / A82 | negative / empty |
| B | e058-p1 | positive / A53 | negative / empty |

Each unpadded handle denotes the corresponding padded catalog handle (A054,
A052, A082, A053). In each case the other annotator and final adjudication
select that same single asset. The notes are unchanged byte for byte as UTF-8
text, and both `language_valid` and `independent_case` remain true. Only the
class and acceptable set were altered. These are five parsing artifacts,
not five annotator disagreements. The adjudications remain positive.

Recomputation over all 600 pairs, replacing only these five stored class/set
answers in memory, gives:

| Measure | Stored judgments | Actual answers for the five cases |
| --- | --- | --- |
| Class agreements | 589/600 (98.1667%) | 594/600 (99%) |
| Class disagreements | 11 | 6 |
| Cohen's kappa | 0.14629414047342054 | 0.2462311557788928 |
| Exact acceptable-set agreements | 451/600 | 456/600 |
| Joint agreements | 451/600 | 456/600 |
| Jaccard sum | 520.55 | 525.55 |
| Language agreements | 600/600 | 600/600 |
| Independence agreements | 599/600 | 599/600 |

The class marginals (positive, negative, ambiguous) change from A=(592,7,1),
B=(595,4,1) to A=(595,4,1), B=(597,2,1). Chance agreement changes from
352269/360000 to 355224/360000; kappa is `(observed - chance)/(1 - chance)`.
All five cases are `en_to_en`: class agreements change 167/175 to 172/175,
acceptable-set and joint agreements each change 131/175 to 136/175, and
Jaccard sum changes 148.91666666666666 to 153.91666666666666. Other lane groups
are unchanged. The record's stored agreement is therefore not raw annotator
agreement and must not be used to approve labelling under section 12.6.6.

The files are preserved because `lock.json.labels.digest` binds the canonical
labels document (`d588ee48071c3d927aceba47d01844ac627d664955ad6c2c4badc58eb7fe872d`).
The preregistered host tasks and episodes bind that corpus lock. The adaptation
bundle also binds its labels document through `inputs.json`, and the record's
labels subject binds the original bytes (SHA-256
`d038766365ecb99c36cad97ebde3d5e752afedf05011d35d58154f0613c096c3`, 737294 bytes).
Changing the judgments and recomputing the lock would invalidate the original
preregistration; it is a new release, not a repair of the historical run.
No labels, agreement fields, subjects, reports or preregistrations were rewritten.

Future annotation resolves only existing catalog handles, including their
unpadded spelling, preserves the answer's class and notes, and rejects
unknown handles without substituting a negative answer. Exact spelling is
retained in original response bytes. Deterministic tests exercise annotation
through the writer without making provider calls.

## Missing drafting bytes

Section 12.6.6 explicitly requires authorship evidence holding the original
drafting request and response bytes. Every case points to
`authorship:drafting-log`, whose bound bytes contain hashes and lengths only.
The loose drafting responses under `logs/draft/` are not catalog/lock-bound
authorship evidence. Requests are absent. Hashes cannot supply those missing
bytes, and neither a recreated request nor a later response is the original.
This independently blocks authorship qualification; the record remains
pending and non-qualifying even if its review fields were filled.

For future runs, the drafting client stores the exact transmitted request
body and original response body. The log writer embeds both losslessly with
hashes and lengths, and the corpus writer marks this log restricted so the
lock binds it and the bundle's private resolver retains it. These changes do
not repair the historical evidence. The other run limitations, including the
unrecorded runtime configuration, remain in the
[run README](owner-run-260/README.md).
