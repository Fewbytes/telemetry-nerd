import { describe, expect, it } from "vitest";
import { caveatText, describeShown, panelNotes } from "./panelNotes";

describe("panelNotes", () => {
  it("turns caveat keys into sentences and keeps unknown keys visible", () => {
    expect(caveatText("low_count", 200)).toContain("fewer than 200");
    expect(caveatText("something_new")).toBe("something_new");
  });
  it("adds the y-scale note as info, after the caveats", () => {
    const notes = panelNotes(["gaps"], { yScaledToData: true, nMin: null });
    expect(notes.map((n) => [n.kind, n.key])).toEqual([
      ["caveat", "gaps"],
      ["info", "y_scaled_to_data"],
    ]);
  });
  it("has no notes when there is nothing to warn about", () => {
    expect(panelNotes([], { yScaledToData: false, nMin: null })).toEqual([]);
  });
});

describe("describeShown", () => {
  it("names the quantile and says it is never aggregated", () => {
    expect(describeShown({ representation: "quantile", quantile: 0.95 }, "1m")).toMatch(/^p95 per 1m window.*never aggregated/);
  });
  it("describes the average-with-envelope default", () => {
    expect(describeShown({ representation: "bucket_agg" }, "30s")).toContain("min–max envelope");
  });
});
