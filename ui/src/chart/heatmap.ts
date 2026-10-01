import type { HeatSeries } from "../lib/api";

export const STRIP_PX = 10; // open buckets: (-Inf, e] and (e, +Inf) get a strip, never a fake range
export type AxisMode = "auto" | "log" | "linear";

export interface ValueAxis {
  kind: "log" | "linear";
  min: number;
  max: number;
  under: boolean; // low strip: (-Inf, e], and on log any bucket reaching <= 0
  over: boolean; // high strip: (e, +Inf)
  length: number;
  body: [number, number];
  pos(v: number): number; // px from the low end
}

export function valueAxis(lo: (number | null)[], hi: (number | null)[], length: number, mode: AxisMode = "auto"): ValueAxis {
  let posMin = Infinity, posMax = -Infinity, linMin = Infinity, linMax = -Infinity;
  let openLow = false, openHigh = false, nonPositive = false;
  for (let i = 0; i < lo.length; i++) {
    const l = lo[i], h = hi[i];
    if (l === null) openLow = true; else { linMin = Math.min(linMin, l); linMax = Math.max(linMax, l); }
    if (h === null) openHigh = true; else { linMin = Math.min(linMin, h); linMax = Math.max(linMax, h); }
    if (l !== null && l > 0) { posMin = Math.min(posMin, l); posMax = Math.max(posMax, h ?? l); }
    else if (l !== null) nonPositive = true;
  }
  const canLog = posMin < Infinity && posMax > posMin;
  // spec §6.2: auto log scale when positive data spans two decades or more
  const log = mode === "log" ? canLog : mode === "auto" && canLog && posMax / posMin >= 100;
  let min = 0, max = 1;
  if (log) { min = posMin; max = posMax; }
  else if (linMin < Infinity) { min = linMin; max = linMax > linMin ? linMax : linMin + 1; }
  const under = openLow || (log && nonPositive);
  const body: [number, number] = [under ? STRIP_PX : 0, length - (openHigh ? STRIP_PX : 0)];
  const frac = log
    ? (v: number) => (Math.log10(v) - Math.log10(min)) / (Math.log10(max) - Math.log10(min))
    : (v: number) => (v - min) / (max - min);
  return {
    kind: log ? "log" : "linear", min, max, under, over: openHigh, length, body,
    pos: (v) => body[0] + frac(v) * (body[1] - body[0]),
  };
}

export function cellSpan(a: ValueAxis, l: number | null, h: number | null): [number, number] {
  if (h === null) return [a.length - STRIP_PX, a.length];
  if (l === null || (a.kind === "log" && l <= 0)) return [0, STRIP_PX];
  return [a.pos(l), a.pos(h)];
}

export interface Span { x: number; w: number }
export interface HeatRect { x: number; y: number; w: number; h: number; t: number; cell: number; lowN: boolean }
export interface HeatLayoutOpts {
  width: number; height: number; startMs: number; endMs: number; stepMs: number;
  nMin: number; color: "count" | "density"; yMode?: AxisMode;
}
export interface HeatLayout {
  axis: ValueAxis; rects: HeatRect[]; missing: Span[]; lowN: Span[];
  colorMax: number; x0Ms: number; spanMs: number; width: number; nAt: Map<number, number>;
}

export function layoutHeatmap(s: HeatSeries, o: HeatLayoutOpts): HeatLayout {
  const axis = valueAxis(s.cells.lo, s.cells.hi, o.height, o.yMode);
  const k = o.stepMs;
  const x0Ms = Math.ceil(o.startMs / k) * k - k; // left edge of the first column
  const x1Ms = Math.ceil(o.endMs / k) * k;
  const spanMs = x1Ms - x0Ms;
  const toX = (ms: number) => ((ms - x0Ms) * o.width) / spanMs;
  const col = (ts: number): Span => ({ x: toX(ts - k), w: toX(ts) - toX(ts - k) });
  const nAt = new Map(s.ts.map((t, i) => [t, s.n[i]]));
  const missing: Span[] = [];
  for (let t = x0Ms + k; t <= x1Ms; t += k) if (!nAt.has(t)) missing.push(col(t));
  const lowN = s.ts.filter((_, i) => s.n[i] > 0 && s.n[i] < o.nMin).map(col);
  const value = (i: number) => {
    if (o.color === "count") return s.cells.c[i];
    const n = nAt.get(s.cells.ts[i]) ?? 0;
    return n > 0 ? s.cells.c[i] / n : 0;
  };
  let colorMax = 0;
  for (let i = 0; i < s.cells.c.length; i++) colorMax = Math.max(colorMax, value(i));
  const scale = (v: number) =>
    colorMax <= 0 ? 0 : o.color === "count" ? Math.log1p(v) / Math.log1p(colorMax) : v / colorMax;
  const rects: HeatRect[] = s.cells.c.map((_, i) => {
    const ts = s.cells.ts[i];
    const [p0, p1] = cellSpan(axis, s.cells.lo[i], s.cells.hi[i]);
    const n = nAt.get(ts) ?? 0;
    return { ...col(ts), y: o.height - p1, h: p1 - p0, t: scale(value(i)), cell: i, lowN: n > 0 && n < o.nMin };
  });
  return { axis, rects, missing, lowN, colorMax, x0Ms, spanMs, width: o.width, nAt };
}

export function hitTest(l: HeatLayout, px: number, py: number): number | null {
  for (let i = l.rects.length - 1; i >= 0; i--) {
    const r = l.rects[i];
    if (px >= r.x && px < r.x + r.w && py >= r.y && py < r.y + r.h) return i;
  }
  return null;
}

export const timeAt = (l: HeatLayout, px: number): number => l.x0Ms + (px / l.width) * l.spanMs;

/** Observations needed for a meaningful quantile: n >= 10/(1-q) (port of exprkind.min_samples). */
export const minSamples = (q: number): number => Math.ceil(Number((10 / (1 - q)).toFixed(6)));

export interface QuantileCell { ts: number; lo: number | null; hi: number | null; cell: number }

/**
 * Per column, the SOURCE bucket that contains quantile q (never an interpolated value),
 * only where the column has enough observations for q to mean anything.
 */
export function quantileCells(s: HeatSeries, q: number): QuantileCell[] {
  const need = minSamples(q);
  const byTs = new Map<number, number[]>();
  s.cells.ts.forEach((t, i) => byTs.set(t, [...(byTs.get(t) ?? []), i]));
  const out: QuantileCell[] = [];
  s.ts.forEach((t, k) => {
    const n = s.n[k];
    const idx = byTs.get(t);
    if (!idx || !(n >= need)) return;
    idx.sort((a, b) => (s.cells.hi[a] ?? Infinity) - (s.cells.hi[b] ?? Infinity) || (s.cells.lo[a] ?? -Infinity) - (s.cells.lo[b] ?? -Infinity));
    const total = idx.reduce((acc, i) => acc + s.cells.c[i], 0);
    let acc = 0;
    for (const i of idx) {
      acc += s.cells.c[i];
      if (acc >= q * total * (1 - 1e-12)) {
        out.push({ ts: t, lo: s.cells.lo[i], hi: s.cells.hi[i], cell: i });
        break;
      }
    }
  });
  return out;
}
