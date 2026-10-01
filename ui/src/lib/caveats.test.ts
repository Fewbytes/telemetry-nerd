import { describe, expect, it } from "vitest";
import type { Finding, Hypothesis } from "./api";
import { supportingCaveats } from "./caveats";

const h = { id: "h1", evidence_for: ["f1"], evidence_against: ["f3"] } as Hypothesis;
const f = (id: string, caveats: string[]) => ({ id, caveats }) as Finding;

describe("supportingCaveats", () => {
  it("lists caveats of supporting findings only", () => {
    const findings = [f("f1", ["synthetic, not an incident"]), f("f2", ["unrelated"]), f("f3", ["against"])];
    expect(supportingCaveats(h, findings)).toEqual([
      { finding: "f1", caveat: "synthetic, not an incident" },
    ]);
  });
  it("is empty when supporting findings have no caveats", () => {
    expect(supportingCaveats(h, [f("f1", [])])).toEqual([]);
  });
});
