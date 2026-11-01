# Success rubric for task:trial-en-001 (DRAFT for owner review)

Status: DRAFT. The owner reviews, edits and adopts this rubric before the freeze.
Once frozen, it is evidence `rubric:task:trial-en-001` and must not change.

The row is a success (`success: true`) only if all of these hold, judged from
the native host session record of that row:

1. The host turn completed (`agent exec` exit 0, a final assistant message).
2. During the turn the host fetched live weather data for Lisbon (a `web_fetch`
   or `exec` tool call whose result contains a forecast), executed by the host.
3. The final answer states the expected rain outlook for Lisbon for the day
   after the request date, consistent with the fetched data.
4. The final answer gives a clear umbrella recommendation that follows from (3).
5. The answer invents no data that is absent from the fetched result.

Anything else is `success: false`. If the session record is missing or
unreadable, the verdict is `null` (unknown), never `true`.

Judging (spec 0.66 section 12.6.6): the owner and the recorded machine
annotator judge each row blind to arm, from anonymized transcripts; the owner
adjudicates disagreements with a reason.
