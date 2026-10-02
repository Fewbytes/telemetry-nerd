import type { HeatSeries } from "../lib/api";
import { STATE } from "./rug";

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
  axis: ValueAxis; rects: HeatRect[]; missing: (Span & { kind: "empty" | "unknown" })[]; lowN: Span[]; partial: Span[];
  colorMax: number; x0Ms: number; spanMs: number; width: number; nAt: Map<number, number>;
}

export function layoutHeatmap(s: HeatSeries, o: HeatLayoutOpts): HeatLayout {
  const axis = valueAxis(s.cells.lo, s.cells.hi, o.height, o.yMode);
  const k = o.stepMs;
  const { x0Ms, x1Ms, spanMs, col } = timeColumns(o.startMs, o.endMs, k, o.width);
  const nAt = new Map(s.ts.map((t, i) => [t, s.n[i]]));
  // no column: UNKNOWN is hatched, anything else (empty, absent, no state) dotted; a blank cell in a returned column is a measured zero
  const stateAt = new Map((s.state?.ts ?? []).map((t, i) => [t, s.state!.state[i]]));
  const missing: (Span & { kind: "empty" | "unknown" })[] = [];
  for (let t = x0Ms + k; t <= x1Ms; t += k)
    if (!nAt.has(t)) missing.push({ ...col(t), kind: stateAt.get(t) === STATE.UNKNOWN ? "unknown" : "empty" });
  const partialTs = new Set([...stateAt].filter(([, v]) => v === STATE.PARTIAL).map(([t]) => t));
  const partial = [...partialTs].map(col);
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
    return { ...col(ts), y: o.height - p1, h: p1 - p0, t: scale(value(i)), cell: i, lowN: (n > 0 && n < o.nMin) || partialTs.has(ts) };
  });
  return { axis, rects, missing, lowN, partial, colorMax, x0Ms, spanMs, width: o.width, nAt };
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

/** Column geometry: column `ts` covers (ts - step, ts]; the first column's left edge snaps down to the step. */
export function timeColumns(startMs: number, endMs: number, stepMs: number, width: number) {
  const k = stepMs;
  const x0Ms = Math.ceil(startMs / k) * k - k;
  const x1Ms = Math.ceil(endMs / k) * k;
  const spanMs = x1Ms - x0Ms;
  const toX = (ms: number) => ((ms - x0Ms) * width) / spanMs;
  return { x0Ms, x1Ms, spanMs, col: (ts: number): Span => ({ x: toX(ts - k), w: toX(ts) - toX(ts - k) }) };
}

/** Inverse of `axis.pos`: the value at a pixel offset. */
export function valueAt(a: ValueAxis, px: number): number {
  const f = (px - a.body[0]) / (a.body[1] - a.body[0]);
  if (a.kind === "linear") return a.min + f * (a.max - a.min);
  const l0 = Math.log10(a.min), l1 = Math.log10(a.max);
  return 10 ** (l0 + f * (l1 - l0));
}

/** Largest colour value on a heatmap, as layoutHeatmap scales it: a bucket count, or its share of its column. */
export function colorMaxOf(series: HeatSeries[], mode: "count" | "density"): number {
  let max = 0;
  for (const s of series) {
    const nAt = new Map(s.ts.map((t, i) => [t, s.n[i]]));
    s.cells.c.forEach((c, i) => {
      const v = mode === "count" ? c : (nAt.get(s.cells.ts[i]) ?? 0) > 0 ? c / (nAt.get(s.cells.ts[i]) as number) : 0;
      if (v > max) max = v;
    });
  }
  return max;
}
