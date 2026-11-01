// RM12/RM39; AC30: browser-safe formatting of already admitted evidence.
import type { RecommendationViewV1 } from "./recommendation-types.ts";
import { numericValue } from "./recommendation-json.ts";

export type RecommendationDisplayOptions = {
  locale?: string;
  // Text output MUST only be assigned to textContent, never HTML or Markdown.
  format?: "text" | "markup-safe";
};
export function neutralizeControls(value: unknown): string {
  return String(value).replace(
    /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu,
    (ch) => "\\u{" + ch.codePointAt(0)!.toString(16) + "}",
  );
}
function escapeMarkup(value: unknown): string {
  return neutralizeControls(value)
    .replace(
      /[\\`*_{}\[\]()<>!#|~:+\-./=&@]/g,
      (ch) => "\\u{" + ch.codePointAt(0)!.toString(16) + "}",
    )
    .replace(/"/g, "\\u{22}");
}
const english = {
  Recommendation: "Recommendation",
  State: "State",
  Decision: "Decision",
  Candidate: "Candidate",
  Kind: "Kind",
  Source: "Source",
  "Source locator": "Source locator",
  "Source version": "Source version",
  "Content hash": "Content hash",
  Availability: "Availability",
  Description: "Description",
  "Recorded score": "Recorded score",
  "Evidence fields": "Evidence fields",
};
const chinese: typeof english = {
  Recommendation: "推荐",
  State: "状态",
  Decision: "决定",
  Candidate: "候选项",
  Kind: "类型",
  Source: "来源",
  "Source locator": "来源位置",
  "Source version": "来源版本",
  "Content hash": "内容哈希",
  Availability: "可用性",
  Description: "描述",
  "Recorded score": "记录分数",
  "Evidence fields": "证据字段",
};
// The native RPC has already validated this scoped receipt. This formatter
// adds no fields and never treats rendered text as action authority.
export function formatRecommendation(
  view: RecommendationViewV1,
  options: RecommendationDisplayOptions = {},
): string {
  const labels = /^zh\b/i.test(options.locale ?? "en-US") ? chinese : english;
  const inert = options.format === "text" ? neutralizeControls : escapeMarkup;
  const lines = [
    labels.Recommendation + ": " + inert(view.recommendation_id),
    labels.State + ": " + inert(view.state),
    labels.Decision + ": " + inert(view.decision),
  ];
  for (const [i, { candidate, asset }] of view.displayed_candidates.entries()) {
    lines.push(
      labels.Candidate + " " + (i + 1) + ": " + inert(candidate.asset_id),
      labels.Kind + ": " + inert(asset.kind),
      labels.Source + ": " + inert(asset.source_id),
      labels["Source locator"] + ": " + inert(asset.source_locator),
      labels["Source version"] + ": " + inert(asset.source_version),
      labels["Content hash"] + ": " + inert(asset.content_hash),
      labels.Availability + ": " + inert(asset.availability),
      labels.Description + ': "' + inert(asset.description) + '"',
      labels["Recorded score"] +
        ": " +
        inert(numericValue(candidate.score)) +
        "; " +
        inert(candidate.score_kind),
      labels["Evidence fields"] +
        ": " +
        candidate.evidence_refs.map(inert).join(", "),
    );
  }
  return lines.join("\n");
}
