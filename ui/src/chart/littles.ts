import type uPlot from "uplot";
import { fmtRange } from "../lib/format";
import { sourceText, type VariationSource } from "../lib/sources";

/**
 * Little's law panel (czt.2, 60j): per window the discrepancy L ÷ λW with its measurement interval
 * and the common-cause envelope, and L and λ·W themselves. Variation is labelled by source:
 * measurement system (the interval; a systematic offset), common cause (small-system / the
 * windows' own variation), special cause (transient windows beyond both).
 */
export type DeviationSource = Exclude<VariationSource, "undetermined">;
type Pair = [number | null, number | null];
export interface LittlesWindow {
  start_ms: number; end_ms: number; n: number;
  verdict: "consistent" | "L_high" | "L_low" | "insufficient" | "no_traffic";
  L: number | null; L_ci: Pair | null;
  lambda_W: number | null; lambda_W_ci: Pair | null;
  ratio: number | null; ci95: Pair | null;
  diff?: number | null; diff_ci?: Pair | null;
  /** common-cause half-width of L and λW at this traffic (relative, 95%) */
  common?: number | null;
  source?: DeviationSource | null;
  /** the level this window is compared with: 1, or the systematic offset */
  reference?: number | null;
  lam?: number | null; W_s?: number | null; flags: string[]; reason?: string;
}
export interface LittlesSystematic {
  direction: "L_high" | "L_low"; ratio: number | null; ci95: Pair | null;
  windows: [number, number]; drifting: boolean;
}
export interface LittlesTransient { index: number; phase: "peak" | "drain" | "other"; source: DeviationSource }
export interface LittlesCommonCause {
  completions_per_window: number | null; rel95: number | null; spread_rel: number | null; warning: string | null;
}
export interface LittlesSeries {
  id: string; labels: Record<string, string>;
  verdict: "consistent" | "L_high" | "L_low" | "inconsistent_in_windows" | "insufficient" | "no_traffic";
  pooled: LittlesWindow; windows: LittlesWindow[];
  reference?: number | null;
  systematic?: LittlesSystematic | null;
  transient?: LittlesTransient[];
  common_cause?: LittlesCommonCause;
}
export interface LittlesUnmatched { labels: Record<string, string>; present_in: string[]; missing_in: string[] }

const VERDICT_TEXT: Record<string, string> = {
  consistent: "consistent",
  L_high: "L higher than λ·W",
  L_low: "L lower than λ·W",
  inconsistent_in_windows: "inconsistent in some windows",
  insufficient: "not enough data",
  no_traffic: "no traffic",
};
export const verdictText = (v: string) => VERDICT_TEXT[v] ?? v;

const n3 = (v: number | null | undefined) => (v == null ? "–" : String(Number(v.toPrecision(3))));
const iv = (c: Pair | null | undefined) => (c ? `[${n3(c[0])}, ${n3(c[1])}]` : "");
const pct = (v: number | null | undefined) => (v == null ? "–" : `${v >= 0 ? "+" : ""}${Math.round(100 * v)}%`);
const signed = (v: number | null | undefined) => (v == null ? "–" : `${v >= 0 ? "+" : ""}${n3(v)}`);

export function seriesTitle(s: LittlesSeries): string {
  const lb = Object.entries(s.labels).map(([k, v]) => `${k}=${v}`).join(", ");
  return lb || "total";
}

/**
 * Window steps: x at each window start plus one closing point at the last end (stepped "after"
 * paths need it). A window that could not be judged, or a gap between windows, is a null row:
 * lines break, never bridge.
 */
function stepRows(s: LittlesSeries, pick: (w: LittlesWindow) => (number | null)[]): { x: number[]; cols: (number | null)[][] } {
  const x: number[] = [];
  let cols: (number | null)[][] = [];
  s.windows.forEach((w, i) => {
    const v = pick(w);
    if (!cols.length) cols = v.map(() => []);
    const prev = s.windows[i - 1];
    if (prev && prev.end_ms < w.start_ms) {
      x.push(prev.end_ms / 1000);
      cols.forEach((c) => c.push(null));
    }
    x.push(w.start_ms / 1000);
    v.forEach((val, k) => cols[k].push(val));
  });
  const last = s.windows[s.windows.length - 1];
  if (last) {
    x.push(last.end_ms / 1000);
    const v = pick(last);
    v.forEach((val, k) => cols[k].push(val));
  }
  return { x, cols };
}

/** Columns: x, L lo, L hi, L, λW lo, λW hi, λW; bands L (2,1) and λW (5,4). */
export function toLittlesUplot(s: LittlesSeries): { data: uPlot.AlignedData; bands: uPlot.Band[] } {
  const { x, cols } = stepRows(s, (w) => [
    w.L_ci?.[0] ?? null, w.L_ci?.[1] ?? null, w.L, w.lambda_W_ci?.[0] ?? null, w.lambda_W_ci?.[1] ?? null, w.lambda_W,
  ]);
  return { data: [x, ...cols] as uPlot.AlignedData, bands: [{ series: [2, 1] }, { series: [5, 4] }] };
}

/** The ratio strip is log-scaled (×2 and ÷2 read alike); values below this are drawn at it. */
export const RATIO_FLOOR = 1 / 16;
const clampR = (v: number | null | undefined) => (v == null ? null : Math.max(RATIO_FLOOR, v));

/** The common-cause envelope's half-width for a window: the small-system scale or the windows' own spread. */
export function envelope(s: LittlesSeries, w: LittlesWindow): number | null {
  const c = w.common ?? null;
  const spread = s.common_cause?.spread_rel ?? 0;
  return c == null ? null : Math.max(c, spread);
}

/**
 * The discrepancy strip. Columns: x, ratio lo, ratio hi (measurement interval), ratio, 1,
 * envelope lo, envelope hi (common cause, around the reference level), reference; bands
 * measurement (2,1) and envelope (6,5). Clamped at RATIO_FLOOR for the log axis.
 */
export function toRatioUplot(s: LittlesSeries): { data: uPlot.AlignedData; bands: uPlot.Band[] } {
  const { x, cols } = stepRows(s, (w) => {
    const ref = w.ratio == null ? null : (w.reference ?? s.reference ?? 1);
    const e = w.ratio == null ? null : envelope(s, w);
    return [
      clampR(w.ci95?.[0]), clampR(w.ci95?.[1]), clampR(w.ratio), 1,
      ref == null || e == null ? null : clampR(ref * (1 - e)), ref == null || e == null ? null : clampR(ref * (1 + e)),
      ref,
    ];
  });
  return { data: [x, ...cols] as uPlot.AlignedData, bands: [{ series: [2, 1] }, { series: [6, 5] }] };
}

/** y range of the ratio strip: always shows ÷2 .. ×2 around 1, wider when the data is. */
export function ratioRange(data: uPlot.AlignedData): [number, number] {
  const vals = (data.slice(1, 4) as (number | null)[][]).flat().filter((v): v is number => v != null);
  return [Math.min(0.5, ...vals), Math.max(2, ...vals)];
}

/** Transient windows (beyond the measurement interval around the reference), in seconds, with their source. */
export function transientSpans(s: LittlesSeries): { x0: number; x1: number; source: DeviationSource; phase: string }[] {
  return (s.transient ?? []).flatMap((t) => {
    const w = s.windows[t.index];
    return w ? [{ x0: w.start_ms / 1000, x1: w.end_ms / 1000, source: t.source, phase: t.phase }] : [];
  });
}

export { sourceText };

/** "systematic offset ×2.1 [1.87, 2.34] in 9/12 windows (measurement system)", or null. */
export function systematicLabel(s: LittlesSeries): string | null {
  const y = s.systematic;
  if (!y || y.ratio == null) return null;
  const drift = y.drifting ? ", drifting" : "";
  return `systematic offset L ÷ λW ${n3(y.ratio)} ${iv(y.ci95)} in ${y.windows[0]}/${y.windows[1]} windows${drift} (measurement system)`;
}

/** Windows judged L_high / L_low, in seconds, for shading. */
export function flaggedSpans(s: LittlesSeries): { x0: number; x1: number; verdict: "L_high" | "L_low" }[] {
  return s.windows
    .filter((w) => w.verdict === "L_high" || w.verdict === "L_low")
    .map((w) => ({ x0: w.start_ms / 1000, x1: w.end_ms / 1000, verdict: w.verdict as "L_high" | "L_low" }));
}


/** Hover text for the window under the cursor (x in seconds). */
export function windowTip(s: LittlesSeries, xs: number): string | null {
  const w = s.windows.find((w) => w.start_ms / 1000 <= xs && xs < w.end_ms / 1000);
  if (!w) return null;
  const head = `${fmtRange(w.start_ms, w.end_ms)} UTC · ${verdictText(w.verdict)}`;
  if (w.ratio == null) return `${head}${w.reason ? `\n${w.reason}` : ""}`;
  const i = s.windows.indexOf(w);
  const t = (s.transient ?? []).find((x) => x.index === i);
  const e = envelope(s, w);
  return [
    head,
    `L − λW ${signed(w.diff)} ${iv(w.diff_ci)} · L ÷ λW ${n3(w.ratio)} ${iv(w.ci95)} (measurement 95%)`,
    `L ${n3(w.L)} ${iv(w.L_ci)} · λ·W ${n3(w.lambda_W)} ${iv(w.lambda_W_ci)}`,
    ...(e != null ? [`common cause ±${Math.round(100 * e)}% around ${n3(w.reference ?? s.reference ?? 1)}`] : []),
    ...(t ? [`TRANSIENT (${sourceText(t.source)}, ${t.phase === "peak" ? "at a load peak" : t.phase === "drain" ? "backlog draining" : "not at a peak"})`]
      : w.source ? [`source: ${sourceText(w.source)}`] : []),
    ...(w.flags.length ? [`flags: ${w.flags.join(", ").replaceAll("_", " ")}`] : []),
  ].join("\n");
}

/** The discrepancy, first: whole range L − λW and L ÷ λW with the measurement interval. */
export function discrepancyText(s: LittlesSeries): string {
  const p = s.pooled;
  if (p.ratio == null) return `whole range: ${p.reason ?? verdictText(p.verdict)}`;
  const rel = p.ci95 ? `${pct(p.ratio - 1)} (${pct((p.ci95[0] ?? 0) - 1)} to ${pct((p.ci95[1] ?? 0) - 1)})` : pct(p.ratio - 1);
  return `L − λW ${signed(p.diff)} · L ÷ λW ${n3(p.ratio)}, ${rel}`;
}

export function littlesLegend(s: LittlesSeries, windowMs: number): string {
  const tr = transientSpans(s);
  const special = tr.filter((t) => t.source === "special_cause").length;
  const cc = s.common_cause;
  const parts = [
    `Whole range ${discrepancyText(s)}`,
    `per ${Math.round(windowMs / 60000)} min window: discrepancy strip L ÷ λW (1 = Little's law holds), dark band = measurement interval (measurement system), light band = common-cause envelope${cc?.rel95 != null ? ` (±${Math.round(100 * cc.rel95)}% at N≈${n3(cc.completions_per_window)}/window)` : ""}`,
    systematicLabel(s) ?? "no systematic offset",
    `${tr.length} transient window${tr.length === 1 ? "" : "s"}${tr.length ? ` (${special} special cause)` : ""} marked`,
    "L = mean in flight (gauge), λ·W = throughput × mean latency (_sum ÷ _count)",
  ];
  return parts.join(" · ");
}
