import { describe, expect, it } from "vitest";
import { seriesName, toUplot } from "./toUplot";
import type { SeriesData } from "../lib/api";

const s = (id: string, labels: Record<string, string>, ts: number[], v: number[]): SeriesData => ({
  id, labels, ts, avg: v, min: v.map((x) => x - 1), max: v.map((x) => x + 1), count: v.map(() => 4),
});

describe("toUplot", () => {
  it("aligns series on the union of timestamps with nulls for gaps", () => {
    const m = toUplot([s("a", { i: "a" }, [1000, 2000], [1, 2]), s("b", { i: "b" }, [2000, 3000], [5, 6])]);
    expect(m.data[0]).toEqual([1, 2, 3]); // seconds
    expect(m.data[1]).toEqual([1, 2, null]); // a avg
    expect(m.data[4]).toEqual([null, 5, 6]); // b avg
    expect(m.points).toBe(6);
  });

  it("puts a null at a hole in the data so the line is not bridged", () => {
    const grid = { start: 1000, end: 4000, step: 1000 };
    const m = toUplot([s("a", { i: "a" }, [1000, 2000, 4000], [1, 2, 4])], grid);
    expect(m.data[0]).toEqual([1, 2, 3, 4]);
    expect(m.data[1]).toEqual([1, 2, null, 4]);
    expect(m.data[2]).toEqual([0, 1, null, 3]); // min
  });

  it("aligns the grid start up to the effective step and keeps off-grid data", () => {
    const m = toUplot([s("a", { i: "a" }, [2000, 6000], [1, 2])], { start: 1500, end: 5000, step: 2000 });
    expect(m.data[0]).toEqual([2, 4, 6]);
    expect(m.data[1]).toEqual([1, null, 2]);
  });

  it("builds a min/max band per series and never spans gaps", () => {
    const m = toUplot([s("a", { i: "a" }, [1000], [1])]);
    expect(m.bands).toEqual([{ series: [3, 2], fill: expect.stringMatching(/^rgba\(/) }]);
    expect(m.series[1].spanGaps).toBe(false);
  });
});

describe("seriesName", () => {
  it("formats labels PromQL-style", () => {
    expect(seriesName({ __name__: "up", job: "api", a: "1" })).toBe('up{a="1",job="api"}');
    expect(seriesName({})).toBe("{}");
  });
});

describe("toUplot quantile mode", () => {
  it("draws quantiles without envelope and fades low-n buckets", () => {
    const q: SeriesData = {
      id: "a", labels: { r: "x" }, ts: [1000, 2000, 3000],
      avg: [34, 0.6, 0.7], min: [34, 0.6, 0.7], max: [34, 0.6, 0.7], count: [13, 2328, 400],
    };
    const m = toUplot([q], undefined, { quantile: true, nMin: 200 });
    expect(m.series.length).toBe(3); // x + solid + faded
    expect(m.bands).toEqual([]);
    expect(m.data[1]).toEqual([null, 0.6, 0.7]);
    expect(m.data[2]).toEqual([34, null, null]);
    expect(String(m.series[2].label)).toContain("n<200");
  });

  it("without nMin draws every value solid", () => {
    const q: SeriesData = {
      id: "a", labels: {}, ts: [1000], avg: [1], min: [1], max: [1], count: [1],
    };
    const m = toUplot([q], undefined, { quantile: true, nMin: null });
    expect(m.series.length).toBe(2);
    expect(m.data[1]).toEqual([1]);
  });
});

describe("toUplot legend", () => {
  it("hides envelope edges from the legend and shows them on the value row", () => {
    const m = toUplot([s("a", { i: "a" }, [1000, 2000], [1, 2])]);
    expect(m.legendHidden).toEqual([2, 3]); // min, max of the only series
    const value = m.series[1].value as (u: unknown, v: number | null, si: number, i: number | null) => string;
    expect(value(null, 1, 1, 0)).toBe("1 [0–2]");
    expect(value(null, null, 1, null)).toBe("--");
  });
  it("hides the faded low-n twin in quantile mode", () => {
    const q: SeriesData = {
      id: "a", labels: {}, ts: [1000], avg: [1], min: [1], max: [1], count: [1],
    };
    expect(toUplot([q], undefined, { quantile: true, nMin: 200 }).legendHidden).toEqual([2]);
  });
});
