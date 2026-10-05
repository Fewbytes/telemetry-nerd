import { describe, expect, it } from "vitest";
import type { SeriesData } from "../lib/api";
import { sliceSeries } from "./preview";

const series = (ts: number[], avg: number[]): SeriesData => ({
  id: "s1", labels: { __name__: "m" }, ts,
  avg, min: avg, max: avg, count: avg.map(() => 1),
});

describe("sliceSeries", () => {
  it("keeps only points within [start_ms, end_ms], inclusive", () => {
    const s = series([0, 1000, 2000, 3000, 4000], [1, 2, 3, 4, 5]);
    const [out] = sliceSeries([s], 1000, 3000);
    expect(out.ts).toEqual([1000, 2000, 3000]);
    expect(out.avg).toEqual([2, 3, 4]);
  });

  it("slices lo/hi alongside the rest when present", () => {
    const s: SeriesData = {
      ...series([0, 1000, 2000], [1, 2, 3]),
      lo: [0, 1, 2], hi: [2, 3, 4],
    };
    const [out] = sliceSeries([s], 1000, 2000);
    expect(out.lo).toEqual([1, 2]);
    expect(out.hi).toEqual([3, 4]);
  });

  it("returns empty arrays, not an error, when nothing falls in range", () => {
    const s = series([0, 1000], [1, 2]);
    const [out] = sliceSeries([s], 5000, 6000);
    expect(out.ts).toEqual([]);
    expect(out.avg).toEqual([]);
  });

  it("preserves id/labels and leaves the input series untouched", () => {
    const s = series([0, 1000], [1, 2]);
    const [out] = sliceSeries([s], 0, 1000);
    expect(out.id).toBe("s1");
    expect(out.labels).toEqual({ __name__: "m" });
    expect(s.ts).toEqual([0, 1000]); // original not mutated
  });
});
