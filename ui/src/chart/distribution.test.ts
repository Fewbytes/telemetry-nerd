import { describe, expect, it } from "vitest";
import { bars, ecdf, maxEcdfGapAtEdges } from "./distribution";
import type { WindowHist } from "../lib/api";

const w = (lo: (number | null)[], hi: (number | null)[], c: number[], label = "w"): WindowHist => ({
  label, start_ms: 0, end_ms: 1, n: c.reduce((a, b) => a + b, 0), columns: 1, lo, hi, c,
});

describe("distribution", () => {
  const a = w([null, 0.1, 1], [0.1, 1, null], [6, 3, 1]);

  it("ecdf is exact at bucket edges and a box inside each bucket", () => {
    expect(ecdf(a)).toEqual([
      { lo: null, hi: 0.1, f0: 0, f1: 0.6 },
      { lo: 0.1, hi: 1, f0: 0.6, f1: 0.9 },
      { lo: 1, hi: null, f0: 0.9, f1: 1 },
    ]);
  });

  it("density is share per decade; open buckets have none", () => {
    const d = bars(a, "density");
    expect(d[0].y).toBeNull();
    expect(d[1].y).toBeCloseTo(0.3);
    expect(bars(a, "count").map((b) => b.y)).toEqual([6, 3, 1]);
    expect(bars(a, "share").map((b) => b.y)).toEqual([0.6, 0.3, 0.1]);
  });

  it("max ECDF gap uses only edges where both ECDFs are exact", () => {
    const b = w([null, 0.1, 1], [0.1, 1, null], [2, 6, 2]);
    const g = maxEcdfGapAtEdges(a, b)!;
    expect(g.at).toBe(0.1);
    expect(g.gap).toBeCloseTo(0.4);
    const coarse = w([0.05], [0.5], [10]); // 0.1 lies inside (0.05, 0.5]: unknown there
    expect(maxEcdfGapAtEdges(a, coarse)?.at).not.toBe(0.1);
  });

  it("empty windows have no ECDF", () => {
    expect(ecdf(w([], [], []))).toEqual([]);
    expect(maxEcdfGapAtEdges(a, w([], [], []))).toBeNull();
  });
});
