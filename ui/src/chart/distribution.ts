import type { WindowHist } from "../lib/api";

export type BarMode = "count" | "share" | "density";
export interface Bar { lo: number | null; hi: number | null; y: number | null }
export interface EcdfStep { lo: number | null; hi: number | null; f0: number; f1: number }

const total = (w: WindowHist) => w.c.reduce((a, b) => a + b, 0);
const order = (w: WindowHist) =>
  w.c.map((_, i) => i).sort((i, j) => (w.hi[i] ?? Infinity) - (w.hi[j] ?? Infinity) || (w.lo[i] ?? -Infinity) - (w.lo[j] ?? -Infinity));

/** Bars on the source buckets (never finer). density = share per decade of value, so
 * unequal buckets compare fairly on a log axis; open/non-positive buckets have no density. */
export function bars(w: WindowHist, mode: BarMode): Bar[] {
  const t = total(w);
  return order(w).map((i) => {
    const lo = w.lo[i], hi = w.hi[i], c = w.c[i];
    if (mode === "count") return { lo, hi, y: c };
    const share = t > 0 ? c / t : 0;
    if (mode === "share") return { lo, hi, y: share };
    return { lo, hi, y: lo !== null && hi !== null && lo > 0 && hi > lo ? share / Math.log10(hi / lo) : null };
  });
}

/** Exact at bucket edges: F(hi) = cumulative/total. Inside a bucket F is only known to lie
 * in [F(lo), F(hi)], drawn as a box, never interpolated (spec §5.1). */
export function ecdf(w: WindowHist): EcdfStep[] {
  const t = total(w);
  if (!(t > 0)) return [];
  let acc = 0;
  return order(w).map((i) => {
    const f0 = acc / t;
    acc += w.c[i];
    return { lo: w.lo[i], hi: w.hi[i], f0, f1: acc / t };
  });
}

/** Largest |Fa(x) - Fb(x)| over bucket edges x where both are exact (x not strictly inside
 * a non-empty bucket of either). A lower bound on the KS distance, not a test. */
export function maxEcdfGapAtEdges(a: WindowHist, b: WindowHist): { gap: number; at: number } | null {
  const ta = total(a), tb = total(b);
  if (!(ta > 0 && tb > 0)) return null;
  const edges = new Set<number>();
  for (const w of [a, b])
    w.c.forEach((_, i) => {
      if (w.lo[i] !== null) edges.add(w.lo[i] as number);
      if (w.hi[i] !== null) edges.add(w.hi[i] as number);
    });
  const exact = (w: WindowHist, t: number, x: number): number | null => {
    let below = 0;
    for (let i = 0; i < w.c.length; i++) {
      const lo = w.lo[i] ?? -Infinity, hi = w.hi[i] ?? Infinity;
      if (lo < x && x < hi) return null;
      if (hi <= x) below += w.c[i];
    }
    return below / t;
  };
  let best: { gap: number; at: number } | null = null;
  for (const x of [...edges].sort((p, q) => p - q)) {
    const fa = exact(a, ta, x), fb = exact(b, tb, x);
    if (fa === null || fb === null) continue;
    const gap = Math.abs(fa - fb);
    if (!best || gap > best.gap) best = { gap, at: x };
  }
  return best;
}
