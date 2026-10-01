import type { DatasetMeta } from "./api";

export interface Note {
  kind: "caveat" | "info";
  key: string;
  text: string;
}

const CAVEATS: Record<string, (nMin: number | null) => string> = {
  gaps: () => "Some time buckets have no data; the line is broken there.",
  low_count: (n) =>
    `Some buckets have fewer than ${n ?? "the required"} observations, so their percentile is not meaningful (drawn faded and dashed).`,
  n_unknown: () =>
    "The number of observations behind each percentile is unknown: do not cite these values as evidence.",
  settling: () => "The newest data is still settling and may change.",
  fake_resolution: () => "The step is finer than the source's scrape interval; the extra resolution is not real.",
  partial: () => "Some incomplete source cells were dropped.",
  empty: () => "The query returned no data.",
  non_finite: () => "Some values were NaN or infinite and are not drawn (their counts are kept).",
};

/** Plain-language caveat; unknown keys are shown as-is rather than hidden. */
export const caveatText = (key: string, nMin: number | null = null): string =>
  (CAVEATS[key] ?? (() => key))(nMin);

/** Warnings and notes about the graph, kept apart from what the graph displays. */
export function panelNotes(caveats: string[], opts: { yScaledToData: boolean; nMin: number | null }): Note[] {
  const notes: Note[] = caveats.map((key) => ({ kind: "caveat", key, text: caveatText(key, opts.nMin) }));
  if (opts.yScaledToData) {
    notes.push({ kind: "info", key: "y_scaled_to_data", text: "The y-axis is scaled to the data (no reference range yet)." });
  }
  return notes;
}

/** One sentence on what the plotted lines are. */
export function describeShown(d: Pick<DatasetMeta, "representation" | "quantile">, step: string): string {
  if (d.representation === "quantile") {
    const q = d.quantile != null ? `p${Number((d.quantile * 100).toFixed(2))}` : "Percentile";
    return `${q} per ${step} window, computed from the histogram at each step and never aggregated.`;
  }
  return `Average per ${step} bucket (line) with its min–max envelope (band).`;
}
