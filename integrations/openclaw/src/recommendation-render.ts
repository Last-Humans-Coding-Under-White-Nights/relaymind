// RM36/RM39: inert evidence text. No source text is interpreted as an instruction.
import { validateRecommendationView } from "./recommendation-contract.ts";
import type { RecommendationViewV1 } from "./recommendation-contract.ts";
function inert(value: unknown): string {
  return String(value)
    .replace(
      /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu,
      (ch) => "\\u{" + ch.codePointAt(0)!.toString(16) + "}",
    )
    .replace(
      /[\\`*_{}\[\]()<>!#|~:+\-./=&@]/g,
      (ch) => "\\u{" + ch.codePointAt(0)!.toString(16) + "}",
    )
    .replace(/"/g, "\\u{22}");
}
export function renderRecommendation(value: RecommendationViewV1): string {
  const view = validateRecommendationView(value);
  const lines = [
    "Recommendation: " + inert(view.recommendation_id),
    "State: " + inert(view.state),
    "Decision: " + inert(view.decision),
  ];
  for (const [i, { candidate, asset }] of view.displayed_candidates.entries()) {
    lines.push(
      "Candidate " + (i + 1) + ": " + inert(candidate.asset_id),
      "Kind: " + inert(asset.kind),
      "Source: " + inert(asset.source_id),
      "Source locator: " + inert(asset.source_locator),
      "Source version: " + inert(asset.source_version),
      "Content hash: " + inert(asset.content_hash),
      "Availability: " + inert(asset.availability),
      'Description: "' + inert(asset.description) + '"',
      "Recorded score: " +
        inert(candidate.score) +
        "; " +
        inert(candidate.score_kind),
      "Evidence fields: " + candidate.evidence_refs.map(inert).join(", "),
    );
  }
  return lines.join("\n");
}
