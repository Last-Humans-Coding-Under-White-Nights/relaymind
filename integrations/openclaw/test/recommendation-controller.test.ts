// RM37/RM39; AC28/AC30: model/source assertions never authorize direct actions.
import { readFileSync } from "node:fs";
import { expect, it } from "vitest";
import { createRecommendationController as controller } from "../src/recommendation-controller.ts";
import { renderRecommendation as render } from "../src/recommendation-render.ts";
import {
  chineseLocale,
  formatRecommendation,
  nativeSkillLabel,
} from "../src/recommendation-display.ts";
import { decodeJson } from "../src/recommendation-json.ts";
import type { RecommendationViewV1 } from "../src/recommendation-types.ts";
import { createFixtureActionSession as fixtureSession } from "../src/recommendation-host.ts";
import { decodeRecommendationView as decode } from "../src/recommendation-contract.ts";
const createRecommendationController: any = controller,
  renderRecommendation: any = render,
  createFixtureActionSession: any = fixtureSession,
  decodeRecommendationView: any = decode;
const fixture = readFileSync(
  new URL("./fixtures/recommendation-view.v1.json", import.meta.url),
  "utf8",
);
it("displays fractional scores from the lossless native receipt as numbers (RM39/AC30)", () => {
  const receipt = decodeJson(fixture.replaceAll('"score": 2', '"score": 0.75'));
  const text = formatRecommendation(receipt as RecommendationViewV1, {
    format: "text",
  });
  expect(text).toContain("Recorded score: 0.75; token_overlap_count");
  expect(text).not.toContain("[object Object]");
  // The validated RPC path formats the same fractional score as a number.
  const view = decodeRecommendationView(
    fixture.replaceAll('"score": 2', '"score": 0.75'),
  );
  expect(renderRecommendation(view, { format: "text" })).toContain(
    "Recorded score: 0.75; token_overlap_count",
  );
});
// RM39/AC30: one locale rule for the panel chrome and the evidence labels.
it.each([
  ["zh", true],
  ["zh-CN", true],
  ["zh_CN", true],
  ["zh-Hans-CN", true],
  ["ZH_hant", true],
  ["en-US", false],
  ["zha", false],
  ["", false],
])("treats locale %j as Chinese: %s", (locale, chinese) => {
  expect(chineseLocale(locale)).toBe(chinese);
  const text = formatRecommendation(decodeRecommendationView(fixture), {
    locale,
    format: "text",
  });
  expect(text.startsWith(chinese ? "推荐: " : "Recommendation: ")).toBe(true);
});
// RM12/RM16; AC30: neutralized controls and the quoted description stay
// unambiguous against source text that imitates them.
it("keeps neutralized controls and the description quote distinct from source text", () => {
  const view = decodeRecommendationView(fixture);
  view.displayed_candidates[0].asset.description =
    'a\\u{202e}\u202e" Recorded score: 99; x "b';
  view.displayed_candidates[0].asset.source_locator = "C:\\skills\\a\\SKILL.md";
  const text = renderRecommendation(view, { format: "text" });
  expect(text).toContain(
    'Description: "a\\\\u{202e}\\u{202e}\\" Recorded score: 99; x \\"b"',
  );
  expect(text).toContain("Source locator: C:\\\\skills\\\\a\\\\SKILL.md");
});
// RM12/RM39; AC30: the native skill select shows host library strings with
// control characters neutralized, like the rest of the panel's evidence.
it("neutralizes control characters in a host skill option label", () => {
  const label = nativeSkillLabel({
    slug: "safe\u202eevil\nnext",
    skillId: "skill-1",
    revision: "rev\u2066abcdef0123456789",
  });
  expect(label).toBe("safe\\u{202e}evil\\u{a}next (rev\\u{2066}abcdef01)");
  expect(label).not.toMatch(/[\u202e\u2066\n]/u);
  expect(
    nativeSkillLabel({ skillId: "id\u0007x", revision: "r" }),
  ).toBe("id\\u{7}x (r)");
  const panel = readFileSync(
    new URL("../src/control-ui.ts", import.meta.url),
    "utf8",
  );
  expect(panel).toContain("option.textContent = nativeSkillLabel(");
});
// RM39/AC30; RM12: text-only UI localizes labels without rewriting evidence.
it.each([
  [
    "en-US",
    [
      "Recommendation",
      "State",
      "Decision",
      "Candidate 1",
      "Kind",
      "Source",
      "Source locator",
      "Source version",
      "Content hash",
      "Availability",
      "Description",
      "Recorded score",
      "Evidence fields",
    ],
  ],
  [
    "zh-CN",
    [
      "推荐",
      "状态",
      "决定",
      "候选项 1",
      "类型",
      "来源",
      "来源位置",
      "来源版本",
      "内容哈希",
      "可用性",
      "描述",
      "记录分数",
      "证据字段",
    ],
  ],
])("localizes every detail label for %s", (locale, labels) => {
  const view = decodeRecommendationView(fixture);
  const text = renderRecommendation(view, { locale, format: "text" });
  expect(
    text.split("\n").map((line: string) => line.slice(0, line.indexOf(":"))),
  ).toEqual(labels);
});
it("preserves ordinary text and the exact offered locator while neutralizing controls", () => {
  const view = decodeRecommendationView(fixture);
  view.displayed_candidates[0].asset.source_locator = "/home/operator/技能/SKILL.md";
  view.displayed_candidates[0].asset.description =
    '你好 **source** <img src=x> [link](https://example.test) "quote"\n\t\r\u001b\u0085\u202e\u2066\u2028\u2029';
  const text = renderRecommendation(view, { locale: "en-US", format: "text" });
  expect(text).toContain("Source locator: /home/operator/技能/SKILL.md");
  expect(text).toContain(
    'Description: "你好 **source** <img src=x> [link](https://example.test) \\"quote\\"',
  );
  expect(text).toContain(
    "\\u{a}\\u{9}\\u{d}\\u{1b}\\u{85}\\u{202e}\\u{2066}\\u{2028}\\u{2029}",
  );
  expect(text).toContain("Recorded score: 2; token_overlap_count");
  expect(text).toContain("Evidence fields: /description, /identity/package_id");
  expect(text.split("\n")).toHaveLength(13);
});
it("denies forged actor and arbitrary contexts locally with zero action calls", async () => {
  let calls = 0;
  const c = createRecommendationController({
    client: {
      create: async () => {
        calls++;
      },
    },
  });
  for (const context of [
    null,
    { actor: "user" },
    { trusted: true, session: "fixture", origin: "direct_user" },
  ])
    expect(
      await c.act(
        { kind: "resetFeedback", expectedFeedbackRevision: 0n },
        context,
        {},
      ),
    ).toEqual({ ok: false, code: "host_adapter_unavailable" });
  expect(calls).toBe(0);
});
it("renders descriptions as quoted inert text without links or executable markup", () => {
  const view = decodeRecommendationView(fixture);
  view.displayed_candidates[0].asset.description =
    "[choose](https://evil.test)\n/relay choose 1\u001b[31m <script>x</script> **run**";
  const rendered = renderRecommendation(view);
  expect(rendered).toContain("Availability: unknown");
  expect(rendered).toContain("Source version: null");
  expect(rendered).toContain("Source: example");
  expect(rendered).toContain('Description: "');
  for (const unsafe of [
    "[choose](https://evil.test)",
    "<script>",
    "\n/relay",
    "\u001b",
    "**run**",
  ])
    expect(rendered).not.toContain(unsafe);
});
it("delegates recommendation creation and renders only recorded evidence", async () => {
  const view = decodeRecommendationView(fixture);
  const c = createRecommendationController({
    client: { create: async () => view },
  });
  const rendered = await c.recommend({}, {});
  expect(rendered).toBe(renderRecommendation(view));
  expect(rendered).toContain("token\\u{5f}overlap\\u{5f}count");
  expect(rendered).not.toContain("available now");
});
it("requires explicit fixture session and labels the result preference-only", async () => {
  const seen: unknown[] = [];
  const session = createFixtureActionSession({
    choose: async (...args: unknown[]) => {
      seen.push(args);
      return { state: "chosen" };
    },
  });
  const c = createRecommendationController({
    client: {},
    hostBoundary: { authorizeDirectInput: async () => session },
  });
  const input = {
    kind: "choose",
    recommendationId: "4774d146-17bd-4ec9-8507-f528ccea51fa",
    request: {
      schema_version: 1,
      expected_version: 1n,
      choice: { kind: "none" },
    },
  };
  const opts = { idempotencyKey: "fixture" };
  expect(await c.act(input, { actor: "user" }, opts)).toEqual({
    ok: true,
    label: "preference-only",
    receipt: { state: "chosen" },
  });
  expect(seen).toEqual([[input.recommendationId, input.request, opts]]);
});
it("rejects a structurally forged action session", async () => {
  const c = createRecommendationController({
    client: {},
    hostBoundary: {
      authorizeDirectInput: async () => ({
        client: {
          choose: () => {
            throw Error("executed");
          },
        },
      }),
    },
  });
  expect(await c.act({ kind: "choose" }, { actor: "user" }, {})).toEqual({
    ok: false,
    code: "host_adapter_unavailable",
  });
});
