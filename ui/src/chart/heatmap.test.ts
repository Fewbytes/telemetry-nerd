import { describe, expect, it } from "vitest";
import { colorMaxOf, hitTest, layoutHeatmap, minSamples, timeAt, valueAt, valueAxis } from "./heatmap";
import type { HeatSeries } from "../lib/api";

const series = (cells: [number, number | null, number | null, number][], ts: number[], n: number[]): HeatSeries => ({
  id: "a", labels: {}, ts, n, cover: ts.map(() => 1),
  cells: { ts: cells.map((c) => c[0]), lo: cells.map((c) => c[1]), hi: cells.map((c) => c[2]), c: cells.map((c) => c[3]) },
});

describe("valueAxis", () => {
  it("is log when positive edges span two decades, with strips for open buckets", () => {
    const a = valueAxis([null, 0.005, 10], [0.005, 0.1, null], 200);
    expect(a.kind).toBe("log");
    expect([a.under, a.over]).toEqual([true, true]);
    expect(a.pos(0.005)).toBeCloseTo(a.body[0]);
    expect(a.pos(10)).toBeCloseTo(a.body[1]);
    expect(a.pos(0.1)).toBeGreaterThan(a.pos(0.005));
  });
  it("stays linear for a narrow range", () => {
    const a = valueAxis([1, 2], [2, 5], 100);
    expect(a.kind).toBe("linear");
    expect([a.under, a.over]).toEqual([false, false]);
    expect(a.pos(1)).toBe(0);
    expect(a.pos(5)).toBe(100);
  });
});

describe("layoutHeatmap", () => {
  const opts = { width: 300, height: 100, startMs: 60_000, endMs: 180_000, stepMs: 60_000, nMin: 20, color: "count" as const };
  it("draws columns as (ts - step, ts]; missing columns differ from zero columns", () => {
    // 60s: 30 obs; 120s: zero (n=0, no cells); 180s: no data
    const l = layoutHeatmap(series([[60_000, 1, 10, 20], [60_000, 10, 100, 10]], [60_000, 120_000], [30, 0]), opts);
    expect(l.rects.map((r) => [r.x, r.w])).toEqual([[0, 100], [0, 100]]);
    expect(l.rects[0]).toMatchObject({ y: 50, h: 50 }); // [1, 10] is the lower half of a log 1..100 axis
    expect(l.missing).toEqual([{ x: 200, w: 100, kind: "empty" }]);
    expect(l.lowN).toEqual([]);
  });
  it("flags low-n columns", () => {
    const l = layoutHeatmap(series([[60_000, 1, 10, 5]], [60_000], [5]), opts);
    expect(l.lowN).toEqual([{ x: 0, w: 100 }]);
    expect(l.rects[0].lowN).toBe(true);
  });
  it("colours by count (monotonic) or by share of the column", () => {
    const s = series([[60_000, 1, 10, 20], [60_000, 10, 100, 10], [120_000, 1, 10, 10]], [60_000, 120_000], [30, 10]);
    const byCount = layoutHeatmap(s, opts).rects.map((r) => r.t);
    expect(byCount[0]).toBe(1);
    expect(byCount[1]).toBeLessThan(byCount[0]);
    expect(layoutHeatmap(s, { ...opts, color: "density" }).rects[2].t).toBe(1);
  });
  it("puts open buckets in strips", () => {
    const l = layoutHeatmap(series([[60_000, null, 0.01, 1], [60_000, 0.01, 10, 1], [60_000, 10, null, 1]], [60_000], [3]), opts);
    expect(l.rects[2]).toMatchObject({ y: 0, h: 10 }); // > 10: top strip
    expect(l.rects[0]).toMatchObject({ y: 90, h: 10 }); // <= 0.01: bottom strip
  });
  it("hit-tests cells and maps x back to time", () => {
    const l = layoutHeatmap(series([[60_000, 1, 10, 20]], [60_000], [20]), opts);
    const r = l.rects[0];
    expect(hitTest(l, r.x + 1, r.y + 1)).toBe(0);
    expect(hitTest(l, 250, 50)).toBeNull();
    expect(timeAt(l, 150)).toBe(90_000);
  });
});

describe("heatmap state textures", () => {
  it("marks missing columns by kind and partial columns", () => {
    const base = series([], [120_000], [5]);
    const st = { ...base, state: { id: base.id, ts: [60_000, 120_000, 180_000], state: [4, 1, 2], observed: [0, 1, 0], expected: [2, 2, 2], flags: [0, 0, 0] } };
    const l = layoutHeatmap(st, { width: 300, height: 100, startMs: 60_000, endMs: 180_000, stepMs: 60_000, nMin: 0, color: "count" });
    expect(l.missing.map((m) => m.kind)).toEqual(["unknown", "empty"]);
    expect(l.partial).toHaveLength(1);
  });
});

describe("axis helpers", () => {
  it("valueAt inverts pos", () => {
    const ax = valueAxis([1], [1000], 100, "log");
    expect(valueAt(ax, ax.pos(31.6))).toBeCloseTo(31.6, 6);
  });
  it("minSamples follows 10/(1-q)", () => {
    expect([0.5, 0.95, 0.99].map(minSamples)).toEqual([20, 200, 1000]);
  });
});

describe("colorMaxOf", () => {
  const sr = series([[60_000, 1, 10, 30], [60_000, 10, 100, 10], [120_000, 1, 10, 5]], [60_000, 120_000], [40, 5]);
  it("is the largest bucket count, or the largest share of a column", () => {
    expect(colorMaxOf([sr], "count")).toBe(30);
    expect(colorMaxOf([sr], "density")).toBe(1); // 5 of 5 in the second column
  });
});
