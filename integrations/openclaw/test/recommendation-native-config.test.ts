// RM37/RM39; AC28/AC30: operator mapping never follows caller identity or resolves secrets while parsing.
import { expect, it } from "vitest";
import { parseNativeRecommendationConfig } from "../src/recommendation-native-config.js";
const binding = {
  profileId: "native-profile",
  authenticatedUserId: "fixture@example.test",
  agentId: "main",
  workspaceDir: "/fixture/native",
  userId: "user",
  workspaceId: "workspace",
  actionTokenEnv: "FIXTURE_ACTION",
};
const configured = () => ({
  controlUiOrigin: "https://fixture.test",
  applicationTokenEnv: "FIXTURE_APPLICATION",
  bindings: [{ ...binding }],
});
it("retains an immutable explicit scope mapping without accessing missing credential values", () => {
  const value = configured();
  const result = parseNativeRecommendationConfig(value);
  expect(result).toEqual({ ...value, skills: [] });
  value.bindings[0].profileId = "later-change";
  expect(result.bindings[0].profileId).toBe("native-profile");
  expect(Object.isFrozen(result.bindings[0])).toBe(true);
});
it.each([
  "extra",
  "body-secret",
  "empty",
  "relative",
  "duplicate-native",
  "duplicate-scope",
  "duplicate-secret",
  "bad-origin",
  "origin-path",
  "bad-reference",
])("refuses %s configuration with a safe error", (kind) => {
  const value: any = configured();
  if (kind === "extra") value.qualified = true;
  if (kind === "body-secret")
    value.bindings[0].actionToken = "do-not-echo-secret";
  if (kind === "empty") value.bindings = [];
  if (kind === "relative") value.bindings[0].workspaceDir = "relative";
  if (kind === "duplicate-native")
    value.bindings.push({
      ...binding,
      userId: "other",
      actionTokenEnv: "OTHER_ACTION",
    });
  if (kind === "duplicate-scope")
    value.bindings.push({
      ...binding,
      profileId: "other",
      actionTokenEnv: "OTHER_ACTION",
    });
  if (kind === "duplicate-secret")
    value.bindings.push({
      ...binding,
      profileId: "other",
      userId: "other",
      actionTokenEnv: "fixture_action",
    });
  if (kind === "bad-origin")
    value.controlUiOrigin = "https://user:do-not-echo-secret@fixture.test";
  if (kind === "origin-path") value.controlUiOrigin += "/path";
  if (kind === "bad-reference")
    value.applicationTokenEnv = "do-not-echo-secret";
  expect(() => parseNativeRecommendationConfig(value)).toThrow(
    "invalid native recommendation configuration",
  );
});
it("binds exact asset/source version to the native library revision rather than renaming its content hash", () => {
  const skill = {
    assetId: "skill:fixture",
    contentHash: "a".repeat(64),
    sourceVersion: null,
    skillId: "4774d146-17bd-4ec9-8507-f528ccea51fa",
    revision: "b".repeat(64),
  };
  expect(
    parseNativeRecommendationConfig({ ...configured(), skills: [skill] })
      .skills,
  ).toEqual([skill]);
  expect(() =>
    parseNativeRecommendationConfig({
      ...configured(),
      skills: [skill, skill],
    }),
  ).toThrow();
  expect(() =>
    parseNativeRecommendationConfig({
      ...configured(),
      skills: [{ ...skill, sourceVersion: true }],
    }),
  ).toThrow();
});

it("accepts the optional native feature in the ordinary provider config without changing its defaults", async () => {
  const { parsePluginConfig } = await import("../src/config.js");
  const parsed = parsePluginConfig({
    baseUrl: "http://127.0.0.1:8080/v1",
    recommendations: configured(),
  });
  expect((parsed as any).recommendations).toEqual({
    ...configured(),
    skills: [],
  });
  expect(parsed.executionMode).toBe("auto");
});
