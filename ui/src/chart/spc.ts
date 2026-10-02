import type uPlot from "uplot";
import { fmtRange } from "../lib/format";

/** SPC panel (bead lkn.1): series, centre line, 3-sigma band from a stated baseline, violations. */
export interface SpcViolation { ts: number; value: number; rules: string[] }
export interface SpcSeries {
  id: string; labels: Record<string, string>; ts: number[]; value: (number | null)[];
  verdict: string; also: string[]; n: number;
  mode: "individuals" | "ar1_residuals" | "insufficient_data"; reason?: string;
  centre?: (number | null)[]; sigma?: number; n_baseline?: number; n_eff_baseline?: number;
  in_control?: boolean | null; violations?: SpcViolation[];
  /** limit uncertainty (99%, from the baseline's n_eff): the level's interval and sigma's */
  level?: number | null; centre_interval?: (number | null)[]; sigma_interval?: (number | null)[];
  /** seasonal part of the centre: none | harmonics | profile | harmonics+profile */
  seasonal?: string;
  seasonal_profile?: { profile: string; expr: string; model: string; history: string; judged_hours_excluded: number };
}
/** kind "reference": a separately fetched earlier window (not in the plotted range). */
export interface SpcBaseline { start_ms: number; end_ms: number; basis: string; kind?: "window" | "reference" }

/** Rules whose points are (near) independent and decide in/out of control. */
export const DECIDING = new Set(["outside_limits", "beyond_3sigma", "ewma", "cusum"]);
const RULE_TEXT: Record<string, string> = {
  outside_limits: "outside the 3σ band",
  beyond_3sigma: "beyond 3σ",
  "2_of_3_beyond_2sigma": "2 of 3 beyond 2σ",
  "4_of_5_beyond_1sigma": "4 of 5 beyond 1σ",
  "8_in_a_row_one_side": "8 in a row on one side",
  ewma: "EWMA",
  cusum: "CUSUM",
};
const ruleText = (r: string) => RULE_TEXT[r] ?? r;
/** What each rule means, for the hover over a flagged point. */
const RULE_DETAIL: Record<string, string> = {
  outside_limits: "outside centre ± 3σ (marginal σ from the baseline)",
  beyond_3sigma: "beyond 3σ of the standardised sequence (deciding)",
  "2_of_3_beyond_2sigma": "2 of the last 3 points beyond 2σ on one side (supplementary)",
  "4_of_5_beyond_1sigma": "4 of the last 5 points beyond 1σ on one side (supplementary)",
  "8_in_a_row_one_side": "8 points in a row on one side of the centre (supplementary)",
  ewma: "EWMA (λ 0.2, L 3) crossed its limit: a small sustained shift (deciding)",
  cusum: "CUSUM (k 0.5, h 5) crossed its limit: a sustained shift (deciding)",
};
const ruleDetail = (r: string) => RULE_DETAIL[r] ?? r;

/** Intervals of the level and sigma, as offsets from the centre curve; null when absent. */
export function limitIntervals(s: SpcSeries): { dlo: number; dhi: number; slo: number; shi: number } | null {
  const [clo, chi] = s.centre_interval ?? [], [slo, shi] = s.sigma_interval ?? [];
  if (s.level == null || clo == null || chi == null || slo == null || shi == null) return null;
  return { dlo: clo - s.level, dhi: chi - s.level, slo, shi };
}

/**
 * Columns: x (s), value, centre, lower 3σ, upper 3σ, then the limits' 99% intervals (upper
 * limit lo/hi, lower limit lo/hi, centre lo/hi: the level's and σ's intervals combined, so
 * each limit inherits both). A null row at each gap: lines break, never interpolate.
 */
export function toSpcUplot(s: SpcSeries, stepMs: number): { data: uPlot.AlignedData; bands: uPlot.Band[] } {
  const cols: (number | null)[][] = Array.from({ length: 10 }, () => []);
  const x: number[] = [];
  const sig = s.sigma ?? null;
  const iv = limitIntervals(s);
  s.ts.forEach((t, i) => {
    if (i > 0 && t - s.ts[i - 1] > stepMs) {
      x.push((s.ts[i - 1] + stepMs) / 1000);
      cols.forEach((c) => c.push(null));
    }
    const ci = s.centre?.[i] ?? null;
    const ok = ci !== null && sig !== null;
    x.push(t / 1000);
    const row = [
      s.value[i], ci, ok ? ci - 3 * sig : null, ok ? ci + 3 * sig : null,
      ok && iv ? ci + iv.dlo + 3 * iv.slo : null, ok && iv ? ci + iv.dhi + 3 * iv.shi : null,
      ok && iv ? ci + iv.dlo - 3 * iv.shi : null, ok && iv ? ci + iv.dhi - 3 * iv.slo : null,
      ok && iv ? ci + iv.dlo : null, ok && iv ? ci + iv.dhi : null,
    ];
    row.forEach((v, k) => cols[k].push(v));
  });
  const bands: uPlot.Band[] = [{ series: [4, 3] }];
  if (iv) bands.push({ series: [6, 5] }, { series: [8, 7] }, { series: [10, 9] });
  return { data: [x, ...cols] as uPlot.AlignedData, bands };
}

/** Baseline window in seconds, clipped to the plotted range (null if outside it). */
export function baselineSpan(b: SpcBaseline, xmin: number, xmax: number): [number, number] | null {
  const a = Math.max(b.start_ms / 1000, xmin), z = Math.min(b.end_ms / 1000, xmax);
  return z > a ? [a, z] : null;
}

export interface SpcMark { x: number; y: number; deciding: boolean; text: string; rules: string[] }

/** Flagged points; `supplementary: false` keeps only those a deciding rule flagged. */
export function violationMarks(s: SpcSeries, supplementary = true): SpcMark[] {
  return (s.violations ?? [])
    .map((p) => ({
      x: p.ts / 1000, y: p.value, deciding: p.rules.some((r) => DECIDING.has(r)),
      text: p.rules.map(ruleText).join(", "), rules: p.rules,
    }))
    .filter((m) => supplementary || m.deciding);
}

/** The mark nearest to the pointer within `radius` px (positions in the same px space). */
export function nearestMark<M extends { x: number; y: number }>(
  marks: M[], px: number, py: number, toPx: (m: M) => [number, number], radius = 6,
): M | null {
  let best: M | null = null, bd = radius * radius;
  for (const m of marks) {
    const [mx, my] = toPx(m);
    const d = (mx - px) ** 2 + (my - py) ** 2;
    if (d <= bd) { bd = d; best = m; }
  }
  return best;
}

/** Hover text for a flagged point: when, value, and every rule it broke with what it means. */
export function markTip(m: SpcMark, stepMs: number): string {
  const t = m.x * 1000;
  return [`${fmtRange(t - stepMs, t)} UTC · ${Number(m.y.toPrecision(4))}`, ...m.rules.map((r) => `• ${ruleDetail(r)}`)].join("\n");
}

export function spcLegend(s: SpcSeries, b: SpcBaseline, supplementary = true): string {
  if (s.mode === "insufficient_data") return `No control limits: ${s.reason ?? "insufficient data"}.`;
  const reference = b.kind === "reference";
  const base = reference
    ? `baseline ${b.basis}, ${fmtRange(b.start_ms, b.end_ms)} UTC, not drawn; every point shown is judged`
    : b.basis === "stated" ? "stated baseline" : "baseline = first half (default)";
  const where = reference ? "" : "shaded ";
  const seq = s.mode === "ar1_residuals"
    ? "run rules, EWMA and CUSUM on AR(1) residuals (autocorrelated)"
    : "run rules, EWMA and CUSUM on the standardised series";
  const nv = (s.violations ?? []).filter((v) => v.rules.some((r) => DECIDING.has(r))).length;
  const state = s.in_control === false ? "out of control" : s.in_control ? "in control" : "not judged";
  const sp = s.seasonal_profile;
  const seasonal = s.seasonal?.includes("profile") && sp
    ? ` · centre follows the operating profile's ${sp.model === "hour_of_week" ? "weekly" : "daily"} shape (${sp.history}, judged hours left out)`
    : s.seasonal?.includes("harmonics") ? " · centre includes a seasonal fit on the baseline" : "";
  const iv = limitIntervals(s);
  const unc = iv ? " · darker strips: 99% intervals of the centre and limits (from n_eff)" : "";
  const marks = supplementary ? "hollow: supplementary run rules" : "supplementary run rules hidden";
  return `Centre and 3σ band from the ${where}${base} only (n ${s.n_baseline}, n_eff ${s.n_eff_baseline}; σ = 1.4826·MAD, marginal)${seasonal}${unc} · ${seq} · ${state}: ${nv} point${nv === 1 ? "" : "s"} flagged by deciding rules (filled), ${marks}; hover a point for its rules`;
}
