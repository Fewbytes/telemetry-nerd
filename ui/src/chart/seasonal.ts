import type uPlot from "uplot";
import { sourceText, type VariationSource } from "../lib/sources";
import { seriesName } from "./toUplot";

/** Seasonal comparison panel (bead lkn.2): now vs previous cycles, their median and 90% band. */
export interface SeasonalCycle { j: number; start_ms: number; values: (number | null)[] }
export interface SeasonalSeries {
  id: string; labels: Record<string, string>; ts: number[]; now: (number | null)[];
  verdict: "usual" | "unusual" | "insufficient_history"; direction: string | null; reasons: string[];
  scheme: "previous" | "1d" | "1w"; label: string; scale: "log" | "linear";
  cycles: SeasonalCycle[]; excluded: { j: number; start_ms: number; reason: string; source?: VariationSource }[]; n: number;
  centre?: (number | null)[]; lo?: (number | null)[]; hi?: (number | null)[];
  ratio?: { kind: "ratio" | "difference"; value: (number | null)[]; lo: (number | null)[]; hi: (number | null)[] };
  flagged?: { ts: number; value: number; z: number }[]; n_eff?: number;
}
export type SeasonalView = "overlay" | "ratio";

/**
 * overlay: x, previous cycles (faint), band lo/hi, centre, now.
 * ratio: x, band lo/hi, the neutral line (1 or 0), now / reference.
 * Nulls stay nulls: lines break at gaps, never interpolated.
 */
export function toSeasonalUplot(s: SeasonalSeries, view: SeasonalView): { data: uPlot.AlignedData; bands: uPlot.Band[]; roles: string[] } {
  const x = s.ts.map((t) => t / 1000);
  if (view === "ratio" && s.ratio) {
    const neutral = s.ratio.kind === "ratio" ? 1 : 0;
    return {
      data: [x, s.ratio.lo, s.ratio.hi, x.map(() => neutral), s.ratio.value] as uPlot.AlignedData,
      bands: [{ series: [2, 1] }],
      roles: ["x", "lo", "hi", "neutral", "now"],
    };
  }
  const cols: (number | null)[][] = s.cycles.map((c) => c.values);
  const roles = ["x", ...s.cycles.map(() => "cycle")];
  const out: (number[] | (number | null)[])[] = [x, ...cols];
  if (s.lo && s.hi && s.centre) {
    out.push(s.lo, s.hi, s.centre);
    roles.push("lo", "hi", "centre");
  }
  out.push(s.now);
  roles.push("now");
  const lo = roles.indexOf("lo"), hi = roles.indexOf("hi");
  return { data: out as uPlot.AlignedData, bands: lo > 0 ? [{ series: [hi, lo] }] : [], roles };
}

const SCHEME_TEXT: Record<string, string> = { previous: "preceding windows", "1d": "previous days", "1w": "previous weeks" };

export function seasonalLegend(s: SeasonalSeries, tz: string, view: SeasonalView): string {
  if (s.verdict === "insufficient_history") return `Not enough history: ${s.reasons[0] ?? ""}.`;
  const excl = s.excluded.length
    ? ` · excluded: ${s.excluded.map((e) => `−${e.j} (${e.reason}${e.source ? `: ${sourceText(e.source)}` : ""})`).join(", ")}`
    : "";
  const band = "90% band = median ± quantiles of leave-one-cycle-out residuals (spread across cycles: the common-cause envelope)";
  const what = view === "ratio"
    ? `now ${s.ratio?.kind === "difference" ? "minus" : "÷"} reference, with the band in the same units`
    : `bold: now · faint: ${s.cycles.length} ${SCHEME_TEXT[s.scheme] ?? s.scheme} · dashed: their median`;
  return `${s.label}${tz !== "UTC" && !s.label.includes(tz) ? ` (${tz})` : ""} · ${what} · ${band}${excl}`;
}

export function flagMarks(s: SeasonalSeries, view: SeasonalView) {
  const at = new Map(s.ts.map((t, i) => [t, i]));
  return (s.flagged ?? []).map((f) => {
    const i = at.get(f.ts) ?? -1;
    const y = view === "ratio" ? (i >= 0 ? s.ratio?.value[i] ?? null : null) : f.value;
    return { x: f.ts / 1000, y, text: `z ${f.z > 0 ? "+" : ""}${f.z}` };
  }).filter((m) => m.y !== null) as { x: number; y: number; text: string }[];
}

/**
 * seriesName({}) renders as the bare braces "{}" (e.g. `sum without()` drops every
 * label including __name__), which reads as a blank prefix ahead of the verdict.
 * Fall back to the dataset expression so there's still a name to show.
 */
export function seriesDisplayName(s: SeasonalSeries, exprFallback: string): string {
  return Object.keys(s.labels).length ? seriesName(s.labels) : exprFallback;
}

export function verdictText(s: SeasonalSeries): string {
  if (s.verdict === "insufficient_history") return "insufficient history";
  if (s.verdict === "usual") return "usual for this time (common cause)";
  return `unusual${s.direction ? ` (${s.direction})` : ""}: special cause`;
}
