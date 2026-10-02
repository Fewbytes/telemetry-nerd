// ui/src/chart/yview.ts — y-views (spec §6.2, bead 2as.17). Pure: no DOM, no uPlot.
import type { SeriesData, YContext, YView } from "../lib/api";
import { fmtValue } from "./axis";
import { ratioRange } from "./indexed";
import { provenance } from "./overlays";

export interface Extent { lo: number; hi: number }
export interface YStats {
  all: Extent | null; meaningful: Extent | null; quantile: boolean; lowN: number; values: number[];
  points?: number[]; // one value per drawn point (the mean), for counting points
}
export interface YResolved {
  range: [number, number] | null; // null = uPlot auto (today's behaviour)
  log: boolean;
  zoomed: boolean; // view is fitted/cropped: "y zoomed" badge + context strip
  clipped: { above: number; below: number; maxAbove: number | null };
  spanPct: number | null; // view height as % of the full data extent
  refused: string | null; // view cannot apply: drawn as auto, reason shown as a caveat
  effective: YView | null; // the view actually applied (a synthetic one when auto resolved to log/reference)
  reference: boolean; // the range includes the normal range / physical limit
  outside: number; // drawn points outside the metric's natural bounds (a contradiction, never hidden)
}
export interface Offer { mode: YView["mode"]; label: string; enabled: boolean; suggest: boolean; title: string; baseline?: "window" | "previous" | "week" }

const ok = (v: number | null): v is number => v !== null && Number.isFinite(v);
const ext = (vs: number[]): Extent | null =>
  vs.length ? { lo: Math.min(...vs), hi: Math.max(...vs) } : null;

export function yStats(series: SeriesData[], o: { quantile: boolean; nMin: number | null }): YStats {
  const values: number[] = [], good: number[] = [], points: number[] = [];
  let lowN = 0;
  for (const s of series)
    s.avg.forEach((v, i) => {
      if (ok(v)) points.push(v);
      if (o.quantile) {
        if (!ok(v)) return;
        values.push(v);
        if (o.nMin === null || (s.count[i] ?? 0) >= o.nMin) good.push(v); else lowN++;
      } else for (const x of [v, s.min[i], s.max[i]]) if (ok(x)) values.push(x);
    });
  return { all: ext(values), meaningful: o.quantile ? ext(good) : null, quantile: o.quantile, lowN, values, points };
}

const hasReference = (c: YContext | null | undefined): boolean => !!c && (!!c.profile || !!c.limit);
const hasBounds = (c: YContext | null | undefined): boolean => !!c && (c.natural_lo !== null || c.natural_hi !== null);

/** The drawn data unioned with the operating (normal) range and the physical limit. */
export function refExtent(all: Extent, c: YContext | null | undefined): Extent {
  let { lo, hi } = all;
  if (c?.profile) { lo = Math.min(lo, c.profile.lo); hi = Math.max(hi, c.profile.hi); }
  if (c?.limit) hi = Math.max(hi, c.limit.hi);
  // every hard bound counts (a threshold is drawn but never stretches the axis)
  for (const l of c?.lines ?? []) if ((l.kind ?? "limit") === "limit") hi = Math.max(hi, l.hi);
  return { lo, hi };
}

/** Never extend below a natural lower bound (or above an upper one). The catalog's word wins;
 *  where it knows nothing, data that never goes negative is assumed not to. */
function pad(e: Extent, c?: YContext | null): [number, number] {
  const span = e.hi - e.lo || Math.abs(e.hi) || 1;
  let lo = e.lo - span * 0.05, hi = e.hi + span * 0.05;
  const floor = c?.bounds ? c.natural_lo : e.lo >= 0 ? 0 : null;
  if (floor !== null && floor !== undefined) lo = Math.max(floor, lo);
  if (c?.natural_hi !== null && c?.natural_hi !== undefined) hi = Math.min(c.natural_hi, hi);
  return [lo, hi];
}
const decades = (e: Extent | null) => (e && e.lo > 0 ? Math.log10(e.hi / e.lo) : 0);
const AUTO: YResolved = {
  range: null, log: false, zoomed: false, clipped: { above: 0, below: 0, maxAbove: null }, spanPct: null,
  refused: null, effective: null, reference: false, outside: 0,
};

function refusal(v: YView, st: YStats, ctx: YContext | null): string | null {
  const a = st.all;
  if (!a) return "no drawn values";
  if (v.mode === "semantic" && !hasBounds(ctx)) return "no natural bounds are known for this metric";
  if (v.mode === "typical" && !ctx?.typical) return "no typical range is known (catalog_scan observes one)";
  if (v.mode === "log" && a.lo <= 0) return `log needs every value > 0 (min ${a.lo})`;
  if (v.mode === "meaningful" && !st.meaningful) return "no bucket has n ≥ n_min";
  if (v.mode === "band" && (v.hi! < a.lo || v.lo! > a.hi)) return "the band contains no data";
  return null;
}

/** The catalog gives both ends: a ratio is [0,1], a percentage [0,100]. */
const closedBounds = (c: YContext | null | undefined): boolean =>
  !!c && c.natural_lo !== null && c.natural_hi !== null;

/** What "auto" means: the metric's fixed natural axis when the catalog bounds it on both sides and
 *  (data outside them stretches the range to show it, and is badged: never autoscaled away); else log when
 *  positive data spans more than two decades; else the reference range when the catalog knows a
 *  normal range or limit; else uPlot's own range. */
function autoView(st: YStats, ctx: YContext | null, noLog = false): YView | null {
  const a = st.all;
  if (a && closedBounds(ctx)) {
    return { mode: "semantic", label: `natural bounds ${ctx!.bounds ?? ""} (auto)`.trim() };
  }
  if (!noLog && a && a.lo > 0 && decades(a) > 2) return { mode: "log", label: `log (auto: ${decades(a).toFixed(1)} decades)` };
  if (a && hasReference(ctx)) return { mode: "reference", label: "reference range" };
  return null;
}

/** `noLog`: the plot cannot draw a log axis (fleet panels), so neither auto nor a chosen log applies. */
export function resolveY(chosen: YView | null, st: YStats, ctx: YContext | null = null, noLog = false): YResolved {
  const v = !chosen || chosen.mode === "auto" || (noLog && chosen.mode === "log") ? autoView(st, ctx, noLog) : chosen;
  if (!v) return AUTO;
  const why = refusal(v, st, ctx);
  if (why) return { ...AUTO, refused: `${v.label}: ${why}` };
  const a = st.all!;
  const ref = hasReference(ctx);
  const base = ref ? refExtent(a, ctx) : a;
  let range: [number, number];
  switch (v.mode) {
    case "zero": range = [Math.min(0, a.lo), Math.max(0, a.hi) + (a.hi - Math.min(0, a.lo)) * 0.05]; break;
    case "data": range = pad(a, ctx); break;
    case "reference": range = pad(base, ctx); break;
    case "semantic": {
      const fit = pad(a, ctx);
      range = [ctx!.natural_lo ?? fit[0], ctx!.natural_hi ?? fit[1]];
      // data beyond a bound is a contradiction: the range reaches it (a hair more, so it is not on the frame)
      const room = (range[1] - range[0]) * 0.005;
      if (ctx!.natural_lo !== null && a.lo < ctx!.natural_lo) range[0] = a.lo - room;
      if (ctx!.natural_hi !== null && a.hi > ctx!.natural_hi) range[1] = a.hi + room;
      break;
    }
    case "meaningful": range = pad(st.meaningful!, ctx); break;
    case "typical": range = pad({ lo: ctx!.typical!.lo, hi: ctx!.typical!.hi }, ctx); break;
    case "band": range = [v.lo!, v.hi!]; break;
    case "log": range = [10 ** Math.floor(Math.log10(a.lo)), 10 ** Math.ceil(Math.log10(a.hi))]; break;
    case "indexed": range = ratioRange(st.values); break;
    default: return { ...AUTO, refused: `${v.label}: unknown view ${v.mode}` };
  }
  const above = st.values.filter((x) => x > range[1]);
  const below = st.values.filter((x) => x < range[0]).length;
  const outside = v.mode === "semantic"
    ? (st.points ?? st.values).filter((x) => (ctx!.natural_lo !== null && x < ctx!.natural_lo) || (ctx!.natural_hi !== null && x > ctx!.natural_hi)).length
    : 0;
  // reference is "zoomed" only when it has nothing beyond the data to show (then it is just a fit)
  const zoomed =
    v.mode === "data" || v.mode === "meaningful" || v.mode === "band" || v.mode === "typical" || (v.mode === "reference" && !ref) ||
    (v.mode !== "indexed" && above.length + below > 0);
  const spanPct = base.hi > base.lo ? Math.min(100, ((range[1] - range[0]) / (base.hi - base.lo)) * 100) : null;
  return {
    range, log: v.mode === "log" || v.mode === "indexed", zoomed, refused: null, spanPct: zoomed ? spanPct : null,
    clipped: { above: above.length, below, maxAbove: above.length ? Math.max(...above) : null },
    effective: v, reference: v.mode === "reference" && ref, outside,
  };
}

export function offeredViews(st: YStats, ctx: YContext | null = null): Offer[] {
  const d = decades(st.all), pos = !!st.all && st.all.lo > 0;
  const out: Offer[] = [
    { mode: "auto", label: "auto", enabled: true, suggest: false,
      title: hasReference(ctx) ? "reference range (data + normal range + physical limit); log when data spans > 2 decades" : "automatic range; log when data spans > 2 decades" },
    { mode: "zero", label: "from zero", enabled: !!st.all, suggest: false, title: "include 0" },
    { mode: "reference", label: "reference range", enabled: !!st.all, suggest: false,
      title: hasReference(ctx) ? "data + normal range + physical limit: small wiggles on a big signal look small" : "no normal range or limit known yet: same as fitting the data" },
    { mode: "semantic", label: "natural bounds", enabled: !!st.all && hasBounds(ctx), suggest: false,
      title: hasBounds(ctx) ? `the metric's natural bounds (${ctx!.bounds})` : "the catalog has no bounds for this metric" },
    { mode: "data", label: "data range", enabled: !!st.all, suggest: false, title: "fit the drawn data (labelled y zoomed)" },
  ];
  if (ctx?.typical)
    out.push({ mode: "typical", label: "typical range", enabled: !!st.all, suggest: false,
      title: `the metric's observed range: ${ctx.typical.basis}` });
  if (st.quantile && st.lowN > 0)
    out.push({ mode: "meaningful", label: "meaningful only", enabled: !!st.meaningful, suggest: false,
      title: `range over buckets with n ≥ n_min only; ${st.lowN} faded bucket(s) may fall outside` });
  out.push({ mode: "log", label: "log", enabled: pos, suggest: pos && d > 2,
    title: pos ? `data spans ${d.toFixed(1)} decades` : "log needs every value > 0" });
  const idxTitle = "each series as a ratio to a common baseline; log axis, 1 centred (instead of a second y axis)";
  out.push(
    { mode: "indexed", baseline: "window", label: "÷ own mean", enabled: !!st.all && !st.quantile, suggest: false,
      title: st.quantile ? "percentiles cannot be averaged over the window; use ÷ last week" : idxTitle },
    { mode: "indexed", baseline: "previous", label: "÷ previous window", enabled: !!st.all, suggest: false, title: idxTitle },
    { mode: "indexed", baseline: "week", label: "÷ last week", enabled: !!st.all, suggest: false, title: idxTitle },
  );
  out.push({ mode: "band", label: "band…", enabled: !!st.all, suggest: false, title: "drag vertically on the plot to pick a y band" });
  return out;
}

export const nonZeroOrigin = (min: number, max: number, log: boolean): boolean => !log && (min > 0 || max < 0);

/** What the context strip is drawn against: the metric's closed natural bounds (a zoom shows where
 *  it sits within them), else the reference extent, else the data. */
export function stripExtent(all: Extent, c: YContext | null | undefined): Extent {
  const base = hasReference(c) ? refExtent(all, c) : all;
  return closedBounds(c) ? { lo: Math.min(base.lo, c!.natural_lo!), hi: Math.max(base.hi, c!.natural_hi!) } : base;
}

export const contextStrip = (all: Extent, r: [number, number]) => {
  const span = all.hi - all.lo || 1, clamp = (x: number) => Math.min(100, Math.max(0, x));
  const b = clamp(((r[0] - all.lo) / span) * 100), t = clamp(((r[1] - all.lo) / span) * 100);
  return { bottomPct: Number(b.toFixed(2)), heightPct: Number((t - b).toFixed(2)) };
};

export function badgeText(v: YView, r: YResolved, unit: string | null, indexLabel?: string, ctx: YContext | null = null): string {
  const parts = v.mode === "indexed" ? ["indexed", indexLabel ?? v.label] : [r.zoomed ? "y zoomed" : r.log ? "log y" : "y", v.label];
  if (r.reference && ctx) {
    const inc = [];
    if (ctx.profile) inc.push(`${ctx.profile.label} ${fmtValue(ctx.profile.lo, unit)}–${fmtValue(ctx.profile.hi, unit)}`);
    const limits = (ctx.lines ?? []).filter((l) => (l.kind ?? "limit") === "limit");
    for (const l of limits.length ? limits : ctx.limit ? [ctx.limit] : []) inc.push(`limit ${l.metric} ${fmtValue(l.hi, unit)}`);
    parts.push(`includes ${inc.join(", ")}`);
  }
  if (ctx && v.mode === "semantic" && ctx.bounds_origin) {
    const why = ctx.bounds_basis ? `; ${ctx.bounds_basis}` : "";
    parts.push(`bounds: ${provenance(ctx.bounds_origin, ctx.bounds_confidence)}${why}`);
  }
  if (ctx?.typical && v.mode === "typical") parts.push(ctx.typical.basis);
  if (r.outside && ctx?.bounds) parts.push(`values outside the physical bounds ${ctx.bounds}: ${r.outside} point${r.outside > 1 ? "s" : ""}`);
  const { above, below, maxAbove } = r.clipped;
  if (above) parts.push(`${above} point${above > 1 ? "s" : ""} above view (max ${fmtValue(maxAbove!, unit)})`);
  if (below) parts.push(`${below} below view`);
  if (!above && !below && r.spanPct !== null && r.spanPct < 100) {
    parts.push(`view spans ${r.spanPct.toFixed(0)}% of ${hasReference(ctx) ? "reference" : "data"} range`);
  }
  return parts.join(" · ");
}
