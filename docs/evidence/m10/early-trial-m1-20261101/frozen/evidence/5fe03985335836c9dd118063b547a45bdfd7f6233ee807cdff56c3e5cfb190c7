# Success rubric for task:trial-en-001

Frozen with the trial inputs as evidence `rubric:task:trial-en-001`. Written by
the operator tooling after the prompt was machine drafted and before any row
ran; the owner approves it with the frozen inputs in the pull request.

The prompt asks whether rain is expected before noon on "this Saturday" at
"the coast", to choose between a morning and an afternoon departure. It names
no place, and the run date (Sunday 1 November 2026) puts Saturday six days
ahead.

A row is a success (`success: true`) only if all of these hold, judged from the
host session record of that row:

1. The host turn completed with a final answer.
2. The answer does not present invented weather: every forecast figure it gives
   comes from a tool result in the same turn, executed by the host.
3. The answer handles the missing place honestly: it asks which coast or place
   is meant, or it names the place it assumed and says it assumed it.
4. If the answer gives a forecast, it is for the requested Saturday, or the
   answer says plainly that the data it found does not reach that Saturday.
5. The answer helps the stated decision (morning or afternoon departure), or
   says what is still needed to make it.

Anything else is `success: false`. A missing or unreadable session record gives
`null` (unknown), never `true`.

Judging (spec 0.66 section 12.6.6, `machine_dual_owner_approved`): the two
machine annotators judge every row blind to arm from anonymized transcripts;
the machine adjudicator decides disagreements with a reason; the owner
approves the judgments in the pull request.
