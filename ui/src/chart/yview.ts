// ui/src/chart/yview.ts — y-views (spec §6.2, bead 2as.17). Pure: no DOM, no uPlot.
import type { SeriesData, YView } from "../lib/api";
import { fmtValue } from "./axis";

export interface Extent { lo: number; hi: number }
export interface YStats {
  all: Extent | null; meaningful: Extent | null; quantile: boolean; lowN: number; values: number[];
}
export interface YResolved {
  range: [number, number] | null; // null = uPlot auto (today's behaviour)
  log: boolean;
  zoomed: boolean; // view is fitted/cropped: "y zoomed" badge + context strip
  clipped: { above: number; below: number; maxAbove: number | null };
  spanPct: number | null; // view height as % of the full data extent
  refused: string | null; // view cannot apply: drawn as auto, reason shown as a caveat
}
export interface Offer { mode: YView["mode"]; label: string; enabled: boolean; suggest: boolean; title: string }

const ok = (v: number | null): v is number => v !== null && Number.isFinite(v);
const ext = (vs: number[]): Extent | null =>
  vs.length ? { lo: Math.min(...vs), hi: Math.max(...vs) } : null;

export function yStats(series: SeriesData[], o: { quantile: boolean; nMin: number | null }): YStats {
  const values: number[] = [], good: number[] = [];
  let lowN = 0;
  for (const s of series)
    s.avg.forEach((v, i) => {
      if (o.quantile) {
        if (!ok(v)) return;
        values.push(v);
        if (o.nMin === null || (s.count[i] ?? 0) >= o.nMin) good.push(v); else lowN++;
      } else for (const x of [v, s.min[i], s.max[i]]) if (ok(x)) values.push(x);
    });
  return { all: ext(values), meaningful: o.quantile ? ext(good) : null, quantile: o.quantile, lowN, values };
}

const pad = (e: Extent): [number, number] => {
  const span = e.hi - e.lo || Math.abs(e.hi) || 1;
  const lo = e.lo - span * 0.05;
  return [e.lo >= 0 ? Math.max(0, lo) : lo, e.hi + span * 0.05]; // natural lower bound (no catalog yet)
};
const decades = (e: Extent | null) => (e && e.lo > 0 ? Math.log10(e.hi / e.lo) : 0);
const AUTO: YResolved = { range: null, log: false, zoomed: false, clipped: { above: 0, below: 0, maxAbove: null }, spanPct: null, refused: null };

function refusal(v: YView, st: YStats): string | null {
  const a = st.all;
  if (!a) return "no drawn values";
  if (v.mode === "log" && a.lo <= 0) return `log needs every value > 0 (min ${a.lo})`;
  if (v.mode === "meaningful" && !st.meaningful) return "no bucket has n ≥ n_min";
  if (v.mode === "band" && (v.hi! < a.lo || v.lo! > a.hi)) return "the band contains no data";
  return null;
}

export function resolveY(v: YView | null, st: YStats): YResolved {
  if (!v || v.mode === "auto") return AUTO;
  const why = refusal(v, st);
  if (why) return { ...AUTO, refused: `${v.label}: ${why}` };
  const a = st.all!;
  let range: [number, number];
  switch (v.mode) {
    case "zero": range = [Math.min(0, a.lo), Math.max(0, a.hi) + (a.hi - Math.min(0, a.lo)) * 0.05]; break;
    case "data": range = pad(a); break;
    case "meaningful": range = pad(st.meaningful!); break;
    case "band": range = [v.lo!, v.hi!]; break;
    case "log": range = [10 ** Math.floor(Math.log10(a.lo)), 10 ** Math.ceil(Math.log10(a.hi))]; break;
    default: return { ...AUTO, refused: `${v.label}: unknown view ${v.mode}` };
  }
  const above = st.values.filter((x) => x > range[1]);
  const below = st.values.filter((x) => x < range[0]).length;
  const zoomed = v.mode === "data" || v.mode === "meaningful" || v.mode === "band" || above.length + below > 0;
  const spanPct = a.hi > a.lo ? Math.min(100, ((range[1] - range[0]) / (a.hi - a.lo)) * 100) : null;
  return {
    range, log: v.mode === "log", zoomed, refused: null, spanPct: zoomed ? spanPct : null,
    clipped: { above: above.length, below, maxAbove: above.length ? Math.max(...above) : null },
  };
}

export function offeredViews(st: YStats): Offer[] {
  const d = decades(st.all), pos = !!st.all && st.all.lo > 0;
  const out: Offer[] = [
    { mode: "auto", label: "auto", enabled: true, suggest: false, title: "uPlot's automatic range" },
    { mode: "zero", label: "from zero", enabled: !!st.all, suggest: false, title: "include 0" },
    { mode: "data", label: "data range", enabled: !!st.all, suggest: false, title: "fit the drawn data (labelled y zoomed)" },
  ];
  if (st.quantile && st.lowN > 0)
    out.push({ mode: "meaningful", label: "meaningful only", enabled: !!st.meaningful, suggest: false,
      title: `range over buckets with n ≥ n_min only; ${st.lowN} faded bucket(s) may fall outside` });
  out.push({ mode: "log", label: "log", enabled: pos, suggest: pos && d > 2,
    title: pos ? `data spans ${d.toFixed(1)} decades` : "log needs every value > 0" });
  out.push({ mode: "band", label: "band…", enabled: !!st.all, suggest: false, title: "drag vertically on the plot to pick a y band" });
  return out;
}

export const nonZeroOrigin = (min: number, max: number, log: boolean): boolean => !log && (min > 0 || max < 0);

export const contextStrip = (all: Extent, r: [number, number]) => {
  const span = all.hi - all.lo || 1, clamp = (x: number) => Math.min(100, Math.max(0, x));
  const b = clamp(((r[0] - all.lo) / span) * 100), t = clamp(((r[1] - all.lo) / span) * 100);
  return { bottomPct: Number(b.toFixed(2)), heightPct: Number((t - b).toFixed(2)) };
};

export function badgeText(v: YView, r: YResolved, unit: string | null): string {
  const parts = [r.zoomed ? "y zoomed" : r.log ? "log y" : "y", v.label];
  const { above, below, maxAbove } = r.clipped;
  if (above) parts.push(`${above} point${above > 1 ? "s" : ""} above view (max ${fmtValue(maxAbove!, unit)})`);
  if (below) parts.push(`${below} below view`);
  if (!above && !below && r.spanPct !== null && r.spanPct < 100) parts.push(`view spans ${r.spanPct.toFixed(0)}% of data range`);
  return parts.join(" · ");
}
