import { describe, expect, it } from "vitest";
import type { WindowHist } from "../lib/api";
import { marginalBars, marginalHeader, scaleBars } from "./marginal";

const W = (lo: (number | null)[], hi: (number | null)[], c: number[]): WindowHist => ({
  label: "now", start_ms: 0, end_ms: 1, n: c.reduce((a, b) => a + b, 0), columns: 1, lo, hi, c,
});
const lin = (v: number) => 100 - v * 3; // value 0 at px 100 (bottom), 30 at px 10 (top)

describe("marginal", () => {
  it("bars follow the y scale and shares are exact", () => {
    const m = marginalBars(W([0, 10, 20], [10, 20, 30], [1, 2, 1]), lin, 10, 100);
    expect(m.bars.map((b) => [b.y0, b.y1, b.share])).toEqual([[70, 100, 0.25], [40, 70, 0.5], [10, 40, 0.25]]);
    expect([m.above, m.below]).toEqual([0, 0]);
  });
  it("open-ended and out-of-view buckets are counted, never drawn with a fake range", () => {
    const m = marginalBars(W([0, 30, 40], [10, 40, null], [2, 1, 1]), lin, 10, 100);
    expect(m.bars).toHaveLength(1);
    expect([m.above, m.below]).toEqual([0.5, 0]); // (30,40] above the view + (40,+Inf)
  });
  it("thin buckets merge into whole-bucket unions of at least minPx", () => {
    const m = marginalBars(W([0, 0.25, 0.5, 0.75], [0.25, 0.5, 0.75, 1], [1, 1, 1, 1]), lin, 10, 100, 3);
    expect(m.bars).toEqual([{ y0: 97, y1: 100, share: 1, density: 1 / 3 }]);
  });
  it("a bucket straddling the view edge is clipped but keeps its true density", () => {
    const m = marginalBars(W([20], [40], [1]), lin, 10, 100);
    expect(m.bars[0]).toMatchObject({ y0: 10, y1: 40, density: 1 / 60 });
  });
  it("log axis: buckets reaching zero or below are counted below the view", () => {
    const log = (v: number) => (v > 0 ? 100 - 30 * Math.log10(v) : NaN);
    const m = marginalBars(W([0, 1], [1, 10], [1, 3]), log, 10, 100);
    expect(m.below).toBe(0.25);
    expect(m.bars).toHaveLength(1);
  });
  it("now and reference share one length scale; header always carries n", () => {
    const a = marginalBars(W([0], [10], [10]), lin, 10, 100), b = marginalBars(W([0, 10], [10, 20], [1, 1]), lin, 10, 100);
    const [la, lb] = scaleBars([a, b], 60);
    expect(la[0]).toBeCloseTo(60);
    expect(lb[0]).toBeCloseTo(30);
    expect(marginalHeader([{ ...W([0], [1], [1200]) }, { ...W([0], [1], [7]), label: "previous window" }], 20))
      .toEqual(["now n=1.2k", "previous window n=7 (too few)"]);
  });
});
