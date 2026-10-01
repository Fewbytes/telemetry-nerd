import { describe, expect, it } from "vitest";
import type { SeriesData } from "../lib/api";
import { fmtRatio, indexSeries, ratioRange, ratioTicks } from "./indexed";

const s = (id: string, avg: (number | null)[], count = avg.map(() => 300)): SeriesData => ({
  id, labels: { instance: id }, ts: avg.map((_, i) => i * 1000), avg,
  min: avg.map((v) => (v === null ? null : v - 0.5)), max: avg.map((v) => (v === null ? null : v + 0.5)), count,
});
const Q = { quantile: false, nMin: null };

describe("indexed", () => {
  it("window baseline divides value and envelope by the series' own mean", () => {
    const r = indexSeries([s("a", [1, 2, 3])], { baseline: "window", label: "1 = mean", values: { a: 2 } }, Q);
    expect(r.series[0].avg).toEqual([0.5, 1, 1.5]);
    expect(r.series[0].min).toEqual([0.25, 0.75, 1.25]);
  });
  it("series without a baseline > 0 are skipped and named; none left is refused", () => {
    const r = indexSeries([s("a", [1]), s("b", [1])], { baseline: "window", label: "", values: { a: 0, b: 2 } }, Q);
    expect(r.series.map((x) => x.id)).toEqual(["b"]);
    expect(r.skipped).toEqual(['{instance="a"}']);
    expect(indexSeries([s("a", [1])], { baseline: "window", label: "", values: {} }, Q).refused).toMatch(/baseline/);
  });
  it("pointwise: matched by series and time; missing, ≤ 0 or low-n baselines are gaps and counted", () => {
    const base = [{ ...s("a", [1, 0, 2, 4], [300, 300, 300, 10]) }];
    const r = indexSeries([s("a", [2, 2, 2, 2])], { baseline: "week", label: "", series: base }, { quantile: true, nMin: 200 });
    expect(r.series[0].avg).toEqual([2, null, 1, null]);
    expect(r.hidden).toBe(2);
  });
  it("ratios ≤ 0 cannot sit on a log axis: hidden and counted", () => {
    const r = indexSeries([s("a", [-1, 2])], { baseline: "window", label: "", values: { a: 1 } }, Q);
    expect(r.series[0].avg).toEqual([null, 2]);
    expect(r.nonPositive).toBe(1);
  });
  it("range is symmetric around 1 on nice multiples; ticks and labels read as multipliers", () => {
    const [lo, hi] = ratioRange([0.8, 1.4]);
    expect(hi).toBe(1.5);
    expect(lo).toBeCloseTo(1 / 1.5);
    expect(ratioTicks(2).map((t) => +t.toFixed(2))).toEqual([0.5, 0.67, 0.8, 1, 1.25, 1.5, 2]);
    expect([fmtRatio(1.5), fmtRatio(2 / 3), fmtRatio(1)]).toEqual(["×1.5", "×0.67", "×1"]);
  });
  it("wide ranges tick on decades", () => {
    expect(ratioTicks(100)).toEqual([0.01, 0.1, 1, 10, 100]);
  });
});
