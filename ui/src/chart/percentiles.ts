import type { HeatSeries } from "../lib/api";
import { cellSpan, minSamples, type Span, type ValueAxis } from "./heatmap";

export const QUANTILE_CHOICES = [0.5, 0.9, 0.95, 0.99, 0.999];
export const DEFAULT_QUANTILES = [0.5, 0.9, 0.99];
export const MAX_QUANTILES = 4;
export const OVERLAY_SERIES = 5;
export const qKey = (q: number): string => String(q); // = Python f"{q:g}"
export const qLabel = (q: number): string => `p${Number((q * 100).toFixed(1))}`;

export interface BandRect extends Span { q: number; ts: number; y: number; h: number; lo: number | null; hi: number | null }

/** The server's per-column source bucket holding q (already n-gated). Higher q first so lower q stays on top. */
export function bandRects(s: HeatSeries, qs: number[], axis: ValueAxis, col: (ts: number) => Span, height: number): BandRect[] {
  const out: BandRect[] = [];
  for (const q of [...qs].sort((a, b) => b - a)) {
    const b = s.quantiles?.[qKey(q)];
    if (!b) continue;
    b.ts.forEach((ts, i) => {
      const [p0, p1] = cellSpan(axis, b.lo[i], b.hi[i]);
      out.push({ q, ts, ...col(ts), y: height - p1, h: p1 - p0, lo: b.lo[i], hi: b.hi[i] });
    });
  }
  return out;
}

/** Columns with data but too few observations for q: no band, a faded marker instead. */
export function thinColumns(s: HeatSeries, q: number, col: (ts: number) => Span): Span[] {
  const need = minSamples(q);
  return s.ts.filter((_, i) => s.n[i] > 0 && s.n[i] < need).map(col);
}

export const overlay = (series: number, qs: number): boolean => qs === 1 && series <= OVERLAY_SERIES;

export function toggleQuantile(qs: number[], q: number): number[] {
  if (qs.includes(q)) return qs.length > 1 ? qs.filter((x) => x !== q) : qs;
  return qs.length >= MAX_QUANTILES ? qs : [...qs, q].sort((a, b) => a - b);
}
