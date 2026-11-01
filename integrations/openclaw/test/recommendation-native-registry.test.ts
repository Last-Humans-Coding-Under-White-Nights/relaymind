// RM37/RM39; AC28/AC30/AC35 (spec 0.65 sections 7.6.1, 7.6.2 condition 9):
// the reviewed registry names each operation separately; only exact entries qualify.
import { expect, it } from "vitest";
import { NATIVE_QUALIFICATIONS } from "../src/recommendation-native-qualification.js";
import type { NativeQualification } from "../src/recommendation-native-qualification.js";
import { registryResolver } from "../src/recommendation-native-registry.js";

const REVISION = "a".repeat(64);
const EVIDENCE = "e".repeat(64);
function entry(extra: Partial<NativeQualification> = {}): NativeQualification {
  return {
    hostVersion: "2026.10.7",
    adapterRevision: REVISION,
    operations: ["direct_actions", "exact_skill_selection"],
    evidenceRevision: EVIDENCE,
    ...extra,
  } as NativeQualification;
}
const mcp = {
  route: "fixture.exact-use",
  transport: "stdio",
  toolScope: "independent-tool",
  maxApprovalWaitMs: 30000,
};

it("ships no skill_loaded_use or mcp_use entry (neither is qualified on the pinned host)", () => {
  for (const item of NATIVE_QUALIFICATIONS) {
    expect(item.operations).not.toContain("skill_loaded_use");
    expect(item.operations).not.toContain("mcp_use");
    expect(item.mcp).toBeUndefined();
  }
});

it("never lets direct_actions or exact_skill_selection imply loaded use or MCP use", () => {
  const resolve = registryResolver([entry()], "2026.10.7", () => REVISION);
  expect(resolve("direct_actions")).toBeDefined();
  expect(resolve("exact_skill_selection")).toBeDefined();
  expect(resolve("skill_loaded_use")).toBeUndefined();
  expect(resolve("mcp_use")).toBeUndefined();
});

it("qualifies a separately listed operation only for the exact host and adapter bytes", () => {
  const listed = [
    entry({ operations: ["skill_loaded_use", "mcp_use"], mcp } as any),
  ];
  expect(
    registryResolver(listed, "2026.10.7", () => REVISION)("skill_loaded_use"),
  ).toBeDefined();
  expect(
    registryResolver(listed, "2026.10.7", () => REVISION)("mcp_use")?.mcp,
  ).toEqual(mcp);
  expect(
    registryResolver(listed, "2026.10.7", () => "b".repeat(64))("mcp_use"),
  ).toBeUndefined();
  expect(
    registryResolver(listed, "2026.10.8", () => REVISION)("mcp_use"),
  ).toBeUndefined();
  expect(
    registryResolver(listed, "2026.10.7", () => {
      throw Error("native_build_required");
    })("skill_loaded_use"),
  ).toBeUndefined();
});

it.each([
  ["mcp_use without its route scope", { operations: ["mcp_use"] }],
  ["a route scope without mcp_use", { operations: ["direct_actions"], mcp }],
  [
    "an unbounded approval wait",
    { operations: ["mcp_use"], mcp: { ...mcp, maxApprovalWaitMs: 0 } },
  ],
  [
    "an infinite approval wait",
    {
      operations: ["mcp_use"],
      mcp: { ...mcp, maxApprovalWaitMs: Number.POSITIVE_INFINITY },
    },
  ],
  ["a malformed evidence revision", { evidenceRevision: "missing" }],
  ["an unknown operation", { operations: ["runtime_snapshots"] }],
  ["an unknown entry field", { qualified: true }],
])("refuses an entry with %s", (_label, extra) => {
  const resolve = registryResolver(
    [entry(extra as any)],
    "2026.10.7",
    () => REVISION,
  );
  for (const operation of [
    "direct_actions",
    "exact_skill_selection",
    "skill_loaded_use",
    "mcp_use",
  ] as const)
    expect(resolve(operation)).toBeUndefined();
});

// Review 4435448011: a non-array registry qualifies nothing instead of throwing.
it.each([null, undefined, {}, "entries"])(
  "fails closed for a non-array registry %s",
  (entries) => {
    const resolve = registryResolver(
      entries as any,
      "2026.10.7",
      () => REVISION,
    );
    expect(resolve("direct_actions")).toBeUndefined();
  },
);
