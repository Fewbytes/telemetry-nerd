import type uPlot from "uplot";
import { fmtRange } from "../lib/format";

/** Little's law panel (bead czt.2): per window L and λ·W with 95% bands, and their ratio. */
export interface LittlesWindow {
  start_ms: number; end_ms: number; n: number;
  verdict: "consistent" | "L_high" | "L_low" | "insufficient" | "no_traffic";
  L: number | null; L_ci: [number | null, number | null] | null;
  lambda_W: number | null; lambda_W_ci: [number | null, number | null] | null;
  ratio: number | null; ci95: [number | null, number | null] | null;
  lam?: number | null; W_s?: number | null; flags: string[]; reason?: string;
}
export interface LittlesSeries {
  id: string; labels: Record<string, string>;
  verdict: "consistent" | "L_high" | "L_low" | "inconsistent_in_windows" | "insufficient" | "no_traffic";
  pooled: LittlesWindow; windows: LittlesWindow[];
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

/** Columns: x, ratio lo, ratio hi, ratio, 1; band (2,1). Clamped at RATIO_FLOOR for the log axis. */
export function toRatioUplot(s: LittlesSeries): { data: uPlot.AlignedData; bands: uPlot.Band[] } {
  const { x, cols } = stepRows(s, (w) => [clampR(w.ci95?.[0]), clampR(w.ci95?.[1]), clampR(w.ratio), 1]);
  return { data: [x, ...cols] as uPlot.AlignedData, bands: [{ series: [2, 1] }] };
}

/** y range of the ratio strip: always shows ÷2 .. ×2 around 1, wider when the data is. */
export function ratioRange(data: uPlot.AlignedData): [number, number] {
  const vals = (data.slice(1, 4) as (number | null)[][]).flat().filter((v): v is number => v != null);
  return [Math.min(0.5, ...vals), Math.max(2, ...vals)];
}

/** Windows judged L_high / L_low, in seconds, for shading. */
export function flaggedSpans(s: LittlesSeries): { x0: number; x1: number; verdict: "L_high" | "L_low" }[] {
  return s.windows
    .filter((w) => w.verdict === "L_high" || w.verdict === "L_low")
    .map((w) => ({ x0: w.start_ms / 1000, x1: w.end_ms / 1000, verdict: w.verdict as "L_high" | "L_low" }));
}

const n3 = (v: number | null | undefined) => (v == null ? "–" : String(Number(v.toPrecision(3))));
const iv = (c: [number | null, number | null] | null) => (c ? `[${n3(c[0])}, ${n3(c[1])}]` : "");

/** Hover text for the window under the cursor (x in seconds). */
export function windowTip(s: LittlesSeries, xs: number): string | null {
  const w = s.windows.find((w) => w.start_ms / 1000 <= xs && xs < w.end_ms / 1000);
  if (!w) return null;
  const head = `${fmtRange(w.start_ms, w.end_ms)} UTC · ${verdictText(w.verdict)}`;
  if (w.ratio == null) return `${head}${w.reason ? `\n${w.reason}` : ""}`;
  return [
    head,
    `L ${n3(w.L)} ${iv(w.L_ci)} · λ·W ${n3(w.lambda_W)} ${iv(w.lambda_W_ci)}`,
    `L ÷ λW ${n3(w.ratio)} ${iv(w.ci95)} (95%)`,
    ...(w.flags.length ? [`flags: ${w.flags.join(", ").replaceAll("_", " ")}`] : []),
  ].join("\n");
}

export function littlesLegend(s: LittlesSeries, windowMs: number): string {
  const p = s.pooled;
  const whole = p.ratio == null
    ? `whole range: ${p.reason ?? verdictText(p.verdict)}`
    : `whole range L ÷ λW ${n3(p.ratio)} ${iv(p.ci95)}`;
  const flagged = flaggedSpans(s).length;
  return `Per ${Math.round(windowMs / 60000)} min window: L = mean in flight (gauge), λ·W = throughput × mean latency (_sum ÷ _count); bands 95% · ratio strip: 1 = Little's law holds · ${whole} · ${flagged} window${flagged === 1 ? "" : "s"} shaded (orange L high: time outside the latency timer; purple L low: missing concurrency)`;
}
