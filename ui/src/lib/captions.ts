// Dense legends as one string each: nerd mode prints them as is, simple mode shows the same text
// as one ellipsised line that opens in place (CompactText), so every number keeps its basis.
import type { SpectrogramPanelData } from "./api";
import { fmtStep } from "./format";

export function spectrumLegend(stepMs: number, significant: number, redNoise: boolean): string {
  return `Lomb-Scargle, linear trend removed · step ${Math.round(stepMs / 1000)}s · dashed: 1% false-alarm level (white noise) · dotted: 1% level against AR(1) red noise (significant = above both)${redNoise ? "; long periods overstated by white noise (autocorrelation)" : ""} · ${significant} significant peak${significant === 1 ? "" : "s"}`;
}

export function spectrogramLegend(sg: Pick<SpectrogramPanelData, "segment_ms" | "hop_ms" | "overlap" | "limits">): string {
  return `window ${fmtStep(sg.segment_ms)} · hop ${fmtStep(sg.hop_ms)} (${Math.round(sg.overlap * 100)}% overlap): each column summarizes one window · period resolution 1/${fmtStep(sg.segment_ms)}, rows show the max within the row · periods ${Math.round(sg.limits.shortest_s)}s–${Math.round(sg.limits.longest_s)}s · colour: share of window variance, linear 0–1 · faint: below the 1% false-alarm level · hatched: window under 50% covered`;
}

export function heatPercentileHint(stepMs: number): string {
  return `bucket holding q per ${fmtStep(stepMs)} column, only where n ≥ 10/(1−q); faded lane: too few observations`;
}

/** The heatmap's quantile outline and the n it needs (10/(1−q)): simple mode hides the picker but not what it drew. */
export function heatOutlineText(q: number): string {
  return `outline: bucket holding p${q * 100} (only where n ≥ ${Math.ceil(10 / (1 - q))})`;
}
