import { describe, expect, it } from "vitest";
import { fmtValue, valueTicks } from "./axis";
import { valueAxis } from "./heatmap";

describe("axis", () => {
  it("puts log ticks on decades inside the range", () => {
    expect(valueTicks(valueAxis([0.005], [10], 200))).toEqual([0.01, 0.1, 1, 10]);
  });
  it("adds 2 and 5 ticks when the range is under two decades", () => {
    expect(valueTicks(valueAxis([1], [100], 200, "log"))).toEqual([1, 2, 5, 10, 20, 50, 100]);
  });
  it("formats seconds below 1 as ms", () => {
    expect(fmtValue(0.25, "s")).toBe("250 ms");
    expect(fmtValue(2.5, "s")).toBe("2.5 s");
    expect(fmtValue(1500, null)).toBe("1500");
  });
});
