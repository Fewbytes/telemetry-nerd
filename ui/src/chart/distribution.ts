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

/** Cumulative views read source buckets: bars may have been merged for width. */
export const exactWindow = (w: WindowHist): WindowHist => (w.source ? { ...w, ...w.source } : w);

export interface QuantileBox { q0: number; q1: number; lo: number | null; hi: number | null; faded: boolean }
/** Inverse ECDF as boxes [F(lo), F(hi)] x [lo, hi]. q is meaningful iff n >= 10/(1-q), i.e. q <= 1 - 10/n. */
export function quantileBoxes(w: WindowHist): { boxes: QuantileBox[]; qMax: number } {
  const qMax = w.n > 0 ? Math.max(0, 1 - 10 / w.n) : 0;
  const boxes: QuantileBox[] = [];
  for (const s of ecdf(w)) {
    if (s.f1 <= qMax + 1e-12) boxes.push({ q0: s.f0, q1: s.f1, lo: s.lo, hi: s.hi, faded: false });
    else if (s.f0 >= qMax - 1e-12) boxes.push({ q0: s.f0, q1: s.f1, lo: s.lo, hi: s.hi, faded: true });
    else boxes.push({ q0: s.f0, q1: qMax, lo: s.lo, hi: s.hi, faded: false }, { q0: qMax, q1: s.f1, lo: s.lo, hi: s.hi, faded: true });
  }
  return { boxes, qMax };
}

/** x position of q: linear, or "nines" -log10(1-q) capped at log10(n) (q = 1 sits at the end). */
export function quantileAxis(mode: "linear" | "nines", nMax: number, length: number) {
  const top = mode === "nines" ? Math.log10(Math.max(nMax, 10)) : 1;
  const f = (q: number) => (mode === "linear" ? q : Math.min(-Math.log10(1 - q), top));
  const ticks = mode === "linear" ? [0, 0.25, 0.5, 0.75, 1] : [0.5, 0.9, 0.99, 0.999, 0.9999].filter((q) => -Math.log10(1 - q) <= top + 1e-9);
  return { pos: (q: number) => (f(q) / top) * length, ticks };
}

export interface SurvivalStep { lo: number | null; hi: number | null; s0: number; s1: number; above: number; faded: boolean }
/** P(X > x): exact at each upper edge (s1), a box [s1, s0] inside the bucket; < 10 observations above: faded. */
export function survival(w: WindowHist): SurvivalStep[] {
  const t = total(w);
  if (!(t > 0)) return [];
  let above = t;
  return order(w).map((i) => {
    const s0 = above / t;
    above -= w.c[i];
    return { lo: w.lo[i], hi: w.hi[i], s0, s1: above / t, above, faded: above < 10 };
  });
}

export type Over =
  | { exact: true; f: number; count: number }
  | { exact: false; min: number; max: number; lo: number | null; hi: number | null };
/** Fraction of observations > x. Exact unless x is strictly inside a non-empty bucket (then bounds). */
export function fractionOver(w: WindowHist, x: number): Over | null {
  const t = total(w);
  if (!(t > 0)) return null;
  let above = 0, inside = 0, lo: number | null = null, hi: number | null = null;
  w.c.forEach((c, i) => {
    const l = w.lo[i] ?? -Infinity, h = w.hi[i] ?? Infinity;
    if (l >= x) above += c;
    else if (x < h && c > 0) { inside += c; lo = w.lo[i]; hi = w.hi[i]; }
  });
  return inside === 0 ? { exact: true, f: above / t, count: above } : { exact: false, min: above / t, max: (above + inside) / t, lo, hi };
}
