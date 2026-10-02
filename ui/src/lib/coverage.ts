import type { WindowHist } from "./api";
import { fmtStep } from "./format";

/** Coverage of a window view (no time axis): a badge only when something is missing. */
export function windowBadge(w: WindowHist, stepMs: number): { text: string; title: string } | null {
  const exp = w.expected_columns ?? w.columns;
  // unknown columns (fetch failed) are not missing ones: reported as "part unknown"
  const miss = Math.max(0, exp - w.columns - (w.unknown_columns ?? 0));
  if (miss === 0 && !w.unknown) return null;
  if (exp <= 0) {
    // nothing was expected (a window with no steps): a percentage of zero is meaningless
    return { text: `${w.label}: no steps to cover · part unknown`, title: "The window has no steps with data to judge; some could not be fetched." };
  }
  const parts = [`${w.label}: covers ${Math.round((100 * w.columns) / exp)}%`];
  if (miss > 0) parts.push(`${fmtStep(miss * stepMs)} missing`);
  if (w.unknown) parts.push("part unknown");
  return {
    text: parts.join(" · "),
    title: `${miss} of ${exp} steps have no data${w.unknown ? `; ${w.unknown_columns ?? "some"} could not be fetched` : ""}. Fractions and counts in this window cover only the observed steps.`,
  };
}
