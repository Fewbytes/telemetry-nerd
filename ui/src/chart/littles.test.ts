import { expect, test } from "vitest";
import { RATIO_FLOOR, flaggedSpans, ratioRange, littlesLegend, seriesTitle, toLittlesUplot, toRatioUplot, verdictText, windowTip, type LittlesSeries, type LittlesWindow } from "./littles";

const w = (i: number, over: Partial<LittlesWindow> = {}): LittlesWindow => ({
  start_ms: i * 300_000, end_ms: (i + 1) * 300_000, n: 20, verdict: "consistent",
  L: 2, L_ci: [1.5, 2.5], lambda_W: 2, lambda_W_ci: [1.9, 2.1], ratio: 1, ci95: [0.7, 1.3], flags: [], ...over,
});
const s: LittlesSeries = {
  id: "total", labels: {}, verdict: "inconsistent_in_windows",
  pooled: w(0, { end_ms: 900_000, ratio: 1.2, ci95: [0.95, 1.4] }),
  windows: [w(0), w(1, { verdict: "L_high", L: 5, ratio: 2.5, ci95: [1.8, 3.2], flags: ["not_steady"] }), w(3)],
};

test("steps: one row per window start, a null row at the gap, a closing row at the last end", () => {
  const m = toLittlesUplot(s);
  expect(m.data[0]).toEqual([0, 300, 600, 900, 1200]);
  expect(m.data[3]).toEqual([2, 5, null, 2, 2]);
  expect(m.bands.map((b) => b.series)).toEqual([[2, 1], [5, 4]]);
  const r = toRatioUplot(s);
  expect(r.data[3]).toEqual([1, 2.5, null, 1, 1]);
  expect(r.data[4]).toEqual([1, 1, null, 1, 1]);
});

test("flagged windows, tip and legend", () => {
  expect(flaggedSpans(s)).toEqual([{ x0: 300, x1: 600, verdict: "L_high" }]);
  const tip = windowTip(s, 400)!;
  expect(tip).toContain("L higher than λ·W");
  expect(tip).toContain("L ÷ λW 2.5 [1.8, 3.2] (95%)");
  expect(tip).toContain("flags: not steady");
  expect(windowTip(s, 700)).toBeNull();
  expect(littlesLegend(s, 300_000)).toContain("whole range L ÷ λW 1.2 [0.95, 1.4]");
  expect(littlesLegend(s, 300_000)).toContain("1 window shaded");
  expect(verdictText("inconsistent_in_windows")).toBe("inconsistent in some windows");
  expect(seriesTitle({ ...s, labels: { instance: "i0" } })).toBe("instance=i0");
  expect(seriesTitle(s)).toBe("total");
});

test("an unjudged window reads its reason", () => {
  const u = { ...s, windows: [w(0, { verdict: "no_traffic", ratio: null, reason: "no arrivals" })] };
  expect(windowTip(u, 10)).toContain("no arrivals");
});

test("ratio strip: log-safe floor and a range that always shows ÷2..×2", () => {
  const z = { ...s, windows: [w(0, { ratio: 0, ci95: [0, 0.2] }), w(1, { ratio: 3, ci95: [2, 5] })] };
  const r = toRatioUplot(z);
  expect(r.data[3]).toEqual([RATIO_FLOOR, 3, 3]);
  expect(ratioRange(r.data)).toEqual([RATIO_FLOOR, 5]);
  expect(ratioRange(toRatioUplot({ ...s, windows: [w(0)] }).data)).toEqual([0.5, 2]);
});
