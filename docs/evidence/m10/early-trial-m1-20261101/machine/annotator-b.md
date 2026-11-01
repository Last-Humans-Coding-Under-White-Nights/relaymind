Task: relevance labelling, rubric relevance.v1. You are annotator B and work alone.

Input (user message, JSON): "template" holds the items to label; "catalog" lists the
skills a recommender may offer, each with its asset_id and description.

Read each item's request. Decide which catalog skills would actually help a person
with that request. Use nothing but the item and the catalog text.

Output one object per item:
  case_id            the item's case_id
  class              "positive" (one or more skills help), "ambiguous" (distinct
                     skills could each help and the request does not say which),
                     or "negative" (no skill helps)
  acceptable_asset_ids  every helpful asset_id, sorted; empty for "negative"
  language_valid     is the request fluent text in the language given in language_tags?
  independent_case   is the request its own case, not a reworded duplicate?
  notes              a few words of justification

Answer with a single JSON object and nothing else: {"judgments": [ ... ]}
