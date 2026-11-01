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
/** The text of a native skill option: host library strings, neutralized. */
export function nativeSkillLabel(entry: {
  slug?: unknown;
  skillId: string;
  revision: string;
}): string {
  return neutralizeControls(
    `${String(entry.slug ?? entry.skillId)} (${entry.revision.slice(0, 12)})`,
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
  recommendation: "Recommendation",
  state: "State",
  decision: "Decision",
  candidate: "Candidate",
  kind: "Kind",
  source: "Source",
  sourceLocator: "Source locator",
  sourceVersion: "Source version",
  contentHash: "Content hash",
  availability: "Availability",
  description: "Description",
  score: "Recorded score",
  evidence: "Evidence fields",
};
const chinese: typeof english = {
  recommendation: "推荐",
  state: "状态",
  decision: "决定",
  candidate: "候选项",
  kind: "类型",
  source: "来源",
  sourceLocator: "来源位置",
  sourceVersion: "来源版本",
  contentHash: "内容哈希",
  availability: "可用性",
  description: "描述",
  score: "记录分数",
  evidence: "证据字段",
};
// The one locale rule for the panel and its evidence: zh, zh-CN, zh_CN, zh-Hans.
export function chineseLocale(locale: unknown): boolean {
  return typeof locale === "string" && /^zh(?:[-_]|$)/i.test(locale);
}
// Text mode doubles a source backslash before neutralizing controls, so a
// source "\u{202e}" never reads like a neutralized control character.
function plainText(value: unknown): string {
  return neutralizeControls(String(value).replace(/\\/g, "\\\\"));
}
// The native RPC has already validated this scoped receipt. This formatter
// adds no fields and never treats rendered text as action authority.
export function formatRecommendation(
  view: RecommendationViewV1,
  options: RecommendationDisplayOptions = {},
): string {
  const labels = chineseLocale(options.locale) ? chinese : english;
  const text = options.format === "text";
  const inert = text ? plainText : escapeMarkup;
  // Escaping the quote keeps the quoted description from imitating a field.
  const quoted = (value: unknown) =>
    text ? plainText(value).replace(/"/g, '\\"') : escapeMarkup(value);
  const lines = [
    labels.recommendation + ": " + inert(view.recommendation_id),
    labels.state + ": " + inert(view.state),
    labels.decision + ": " + inert(view.decision),
  ];
  for (const [i, { candidate, asset }] of view.displayed_candidates.entries()) {
    lines.push(
      labels.candidate + " " + (i + 1) + ": " + inert(candidate.asset_id),
      labels.kind + ": " + inert(asset.kind),
      labels.source + ": " + inert(asset.source_id),
      labels.sourceLocator + ": " + inert(asset.source_locator),
      labels.sourceVersion + ": " + inert(asset.source_version),
      labels.contentHash + ": " + inert(asset.content_hash),
      labels.availability + ": " + inert(asset.availability),
      labels.description + ': "' + quoted(asset.description) + '"',
      labels.score +
        ": " +
        inert(numericValue(candidate.score)) +
        "; " +
        inert(candidate.score_kind),
      labels.evidence + ": " + candidate.evidence_refs.map(inert).join(", "),
    );
  }
  return lines.join("\n");
}
