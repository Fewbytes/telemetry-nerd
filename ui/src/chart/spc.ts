import type uPlot from "uplot";

/** SPC panel (bead lkn.1): series, centre line, 3-sigma band from a stated baseline, violations. */
export interface SpcViolation { ts: number; value: number; rules: string[] }
export interface SpcSeries {
  id: string; labels: Record<string, string>; ts: number[]; value: (number | null)[];
  verdict: string; also: string[]; n: number;
  mode: "individuals" | "ar1_residuals" | "insufficient_data"; reason?: string;
  centre?: (number | null)[]; sigma?: number; n_baseline?: number; n_eff_baseline?: number;
  in_control?: boolean | null; violations?: SpcViolation[];
}
export interface SpcBaseline { start_ms: number; end_ms: number; basis: string }

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
export const ruleText = (r: string) => RULE_TEXT[r] ?? r;

/** Columns: x (s), value, centre, lower 3σ, upper 3σ. A null row at each gap: lines break, never interpolate. */
export function toSpcUplot(s: SpcSeries, stepMs: number): { data: uPlot.AlignedData; bands: uPlot.Band[] } {
  const x: number[] = [], v: (number | null)[] = [], c: (number | null)[] = [], lo: (number | null)[] = [], hi: (number | null)[] = [];
  const sig = s.sigma ?? null;
  s.ts.forEach((t, i) => {
    if (i > 0 && t - s.ts[i - 1] > stepMs) {
      x.push((s.ts[i - 1] + stepMs) / 1000); v.push(null); c.push(null); lo.push(null); hi.push(null);
    }
    const ci = s.centre?.[i] ?? null;
    x.push(t / 1000); v.push(s.value[i]); c.push(ci);
    lo.push(ci !== null && sig !== null ? ci - 3 * sig : null);
    hi.push(ci !== null && sig !== null ? ci + 3 * sig : null);
  });
  return { data: [x, v, c, lo, hi] as uPlot.AlignedData, bands: [{ series: [4, 3] }] };
}

/** Baseline window in seconds, clipped to the plotted range (null if outside it). */
export function baselineSpan(b: SpcBaseline, xmin: number, xmax: number): [number, number] | null {
  const a = Math.max(b.start_ms / 1000, xmin), z = Math.min(b.end_ms / 1000, xmax);
  return z > a ? [a, z] : null;
}

export function violationMarks(s: SpcSeries) {
  return (s.violations ?? []).map((p) => ({
    x: p.ts / 1000, y: p.value, deciding: p.rules.some((r) => DECIDING.has(r)),
    text: p.rules.map(ruleText).join(", "),
  }));
}

export function spcLegend(s: SpcSeries, b: SpcBaseline): string {
  if (s.mode === "insufficient_data") return `No control limits: ${s.reason ?? "insufficient data"}.`;
  const base = b.basis === "stated" ? "stated baseline" : "baseline = first half (default)";
  const seq = s.mode === "ar1_residuals"
    ? "run rules, EWMA and CUSUM on AR(1) residuals (autocorrelated)"
    : "run rules, EWMA and CUSUM on the standardised series";
  const nv = (s.violations ?? []).filter((v) => v.rules.some((r) => DECIDING.has(r))).length;
  const state = s.in_control === false ? "out of control" : s.in_control ? "in control" : "not judged";
  return `Centre and 3σ band from the shaded ${base} only (n ${s.n_baseline}, n_eff ${s.n_eff_baseline}; σ = 1.4826·MAD, marginal) · ${seq} · ${state}: ${nv} point${nv === 1 ? "" : "s"} flagged by deciding rules (filled), hollow: supplementary run rules`;
}
