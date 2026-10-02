import type { WindowHist } from "./api";
import { fmtStep } from "./format";

/** Coverage of a window view (no time axis): a badge only when something is missing. */
export function windowBadge(w: WindowHist, stepMs: number): { text: string; title: string } | null {
  const exp = w.expected_columns ?? w.columns;
  const miss = Math.max(0, exp - w.columns);
  if (miss === 0 && !w.unknown) return null;
  const parts = [`${w.label}: covers ${Math.round((100 * w.columns) / Math.max(1, exp))}%`];
  if (miss > 0) parts.push(`${fmtStep(miss * stepMs)} missing`);
  if (w.unknown) parts.push("part unknown");
  return {
    text: parts.join(" · "),
    title: `${miss} of ${exp} steps have no data${w.unknown ? "; some could not be fetched" : ""}. Fractions and counts in this window cover only the observed steps.`,
  };
}
