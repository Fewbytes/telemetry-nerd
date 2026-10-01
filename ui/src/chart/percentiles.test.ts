import { describe, expect, it } from "vitest";
import { bandRects, overlay, qLabel, thinColumns, toggleQuantile } from "./percentiles";
import { timeColumns, valueAxis } from "./heatmap";
import type { HeatSeries } from "../lib/api";

const s: HeatSeries = {
  id: "a", labels: {}, ts: [60_000, 120_000, 180_000], n: [300, 150, 0], cover: [1, 1, 1],
  cells: { ts: [], lo: [], hi: [], c: [] },
  quantiles: { "0.5": { ts: [60_000, 120_000], lo: [1, 1], hi: [10, 10] }, "0.95": { ts: [60_000], lo: [10], hi: [null] } },
};
const { col } = timeColumns(60_000, 180_000, 60_000, 300);

describe("percentile bands", () => {
  const axis = valueAxis([1, 10], [10, null], 100, "log");
  it("draws the server's source bucket per column, open top bucket in the strip", () => {
    const r = bandRects(s, [0.5, 0.95], axis, col, 100);
    expect(r.map((x) => [x.q, x.ts])).toEqual([[0.95, 60_000], [0.5, 60_000], [0.5, 120_000]]); // high q drawn first
    expect(r[0].y).toBe(0); // hi null -> top strip
    expect(r[1].x).toBe(0);
    expect(r[1].w).toBeCloseTo(100);
  });
  it("columns with data but n < minSamples(q) are thin, empty columns are not", () => {
    expect(thinColumns(s, 0.95, col).map((c) => c.x)).toEqual([100]); // n=150 < 200; n=0 is not thin
    expect(thinColumns(s, 0.5, col)).toEqual([]);
  });
  it("overlay only one q over <= 5 series", () => {
    expect(overlay(5, 1)).toBe(true);
    expect(overlay(6, 1)).toBe(false);
    expect(overlay(2, 2)).toBe(false);
  });
  it("toggles keep 1..4 sorted quantiles", () => {
    expect(toggleQuantile([0.5, 0.99], 0.9)).toEqual([0.5, 0.9, 0.99]);
    expect(toggleQuantile([0.5], 0.5)).toEqual([0.5]);
    expect(toggleQuantile([0.5, 0.9, 0.95, 0.99], 0.999)).toEqual([0.5, 0.9, 0.95, 0.99]);
    expect(qLabel(0.999)).toBe("p99.9");
  });
});
