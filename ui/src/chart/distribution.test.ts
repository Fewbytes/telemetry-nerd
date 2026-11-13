import { describe, expect, it } from "vitest";
import {
  bars, ecdf, exactWindow, fractionOver, maxEcdfGapAtEdges, qqBoxes, qqRejection, quantileAxis, quantileBoxes, survival,
} from "./distribution";
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

describe("cumulative views", () => {
  const a = w([null, 0.1, 1], [0.1, 1, null], [60, 30, 10]); // n = 100
  it("quantile boxes fade beyond q_max = 1 - 10/n", () => {
    const b = quantileBoxes(a);
    expect(b.qMax).toBeCloseTo(0.9);
    expect(b.boxes.map((x) => [x.q0, x.q1, x.faded])).toEqual([[0, 0.6, false], [0.6, 0.9, false], [0.9, 1, true]]);
  });
  it("a box crossing q_max is split", () => {
    const b = quantileBoxes(w([0, 1], [1, 2], [85, 15]));
    expect(b.boxes.map((x) => [x.q0, x.q1, x.faded])).toEqual([[0, 0.85, false], [0.85, 0.9, false], [0.9, 1, true]]);
  });
  it("nines axis caps at log10(n)", () => {
    const ax = quantileAxis("nines", 1000, 300);
    expect(ax.pos(0.99)).toBeCloseTo(200);
    expect(ax.pos(1)).toBe(300);
    expect(ax.ticks).toEqual([0.5, 0.9, 0.99, 0.999]);
  });
  it("survival is exact at upper edges; thin tails fade", () => {
    const got = survival(a);
    expect(got.map((r) => r.above)).toEqual([40, 10, 0]);
    expect(got.map((r) => r.s1)).toEqual([expect.closeTo(0.4, 9), expect.closeTo(0.1, 9), 0]);
    expect(got.map((r) => r.s0)).toEqual([1, expect.closeTo(0.4, 9), expect.closeTo(0.1, 9)]);
    expect(got.map((r) => r.faded)).toEqual([false, false, true]);
  });
  it("fraction over: exact at edges, bounded inside a bucket", () => {
    expect(fractionOver(a, 1)).toEqual({ exact: true, f: 0.1, count: 10 });
    expect(fractionOver(a, 0.1)).toEqual({ exact: true, f: 0.4, count: 40 });
    expect(fractionOver(a, 0.5)).toEqual({ exact: false, min: 0.1, max: 0.4, lo: 0.1, hi: 1 });
  });
  it("cumulative views use source buckets when bars were merged", () => {
    const m = { ...a, source: { lo: [null, 0.1, 0.5, 1], hi: [0.1, 0.5, 1, null], c: [60, 20, 10, 10] } };
    expect(exactWindow(m).c).toEqual([60, 20, 10, 10]);
    expect(exactWindow(a)).toBe(a);
  });
});

describe("Q-Q", () => {
  const edges = { lo: [0, 1, 2, 3], hi: [1, 2, 3, 4] };
  const a = w(edges.lo, edges.hi, [250, 250, 250, 250]); // n = 1000
  const shifted = w([1, 2, 3, 4], [2, 3, 4, 5], [250, 250, 250, 250]); // a + 1

  it("identical samples lie on y = x: every box pairs a bucket with itself", () => {
    const { boxes } = qqBoxes(a, a);
    expect(boxes.length).toBe(4 + 1); // 4 buckets, the last split at q_max = 0.99
    for (const b of boxes) expect(b.b).toEqual(b.a);
  });

  it("a sample shifted by one bucket sits one unit above y = x", () => {
    const { boxes } = qqBoxes(a, shifted);
    for (const b of boxes) {
      expect(b.b.lo).toBe((b.a.lo as number) + 1);
      expect(b.b.hi).toBe((b.a.hi as number) + 1);
    }
  });

  it("a scaled sample (x2 per bucket edge) gives a line of slope 2 through the origin", () => {
    const scaled = w([0, 2, 4, 6], [2, 4, 6, 8], [250, 250, 250, 250]);
    for (const b of qqBoxes(a, scaled).boxes) {
      expect(b.b.lo).toBe(2 * (b.a.lo as number));
      expect(b.b.hi).toBe(2 * (b.a.hi as number));
    }
  });

  it("splits at the other window's bucket edges and fades beyond the thinner window's q_max", () => {
    const coarse = w([0], [4], [20]); // n = 20: q_max = 0.5
    const { boxes, qMax } = qqBoxes(a, coarse);
    expect(qMax).toBeCloseTo(0.5);
    expect(boxes.map((x) => [x.q0, x.q1, x.a.hi, x.b.hi, x.faded])).toEqual([
      [0, 0.25, 1, 4, false], [0.25, 0.5, 2, 4, false], [0.5, 0.75, 3, 4, true], [0.75, 1, 4, 4, true],
    ]);
  });

  it("rejects a window with fewer than 2 observations, naming it", () => {
    expect(qqRejection(a, shifted)).toBeNull();
    expect(qqRejection(a, w([0], [1], [1], "tiny"))).toMatch(/at least 2 observations.*tiny has fewer/);
    expect(qqRejection(w([], [], [], "none"), w([0], [1], [0], "zero"))).toMatch(/none and zero have fewer/);
  });
});
