// RM36/RM39: validate evidence before formatting it for the RPC/controller.
import { validateRecommendationView } from "./recommendation-contract.ts";
import type { RecommendationViewV1 } from "./recommendation-contract.ts";
import { formatRecommendation } from "./recommendation-display.ts";
import type { RecommendationDisplayOptions } from "./recommendation-display.ts";

export function renderRecommendation(
  value: RecommendationViewV1,
  options: RecommendationDisplayOptions = {},
): string {
  return formatRecommendation(validateRecommendationView(value), options);
}
