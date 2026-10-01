import type { DatasetMeta } from "./api";

export interface Note {
  kind: "caveat" | "info";
  key: string;
  text: string;
}

type Describe = (nMin: number | null, dist: boolean) => string;

const CAVEATS: Record<string, Describe> = {
  gaps: (_n, dist) =>
    dist
      ? "Some steps have no data; those columns are hatched (no data is not zero)."
      : "Some time buckets have no data; the line is broken there.",
  low_count: (n, dist) =>
    dist
      ? `Columns with fewer than ${n ?? "the required"} observations are dimmed and ticked: their shape is noise.`
      : `Some buckets have fewer than ${n ?? "the required"} observations, so their percentile is not meaningful (drawn faded and dashed).`,
  n_unknown: () =>
    "The number of observations behind each percentile is unknown: do not cite these values as evidence.",
  settling: () => "The newest data is still settling and may change.",
  fake_resolution: () => "The step is finer than the source's scrape interval; the extra resolution is not real.",
  partial: () => "Some incomplete source cells were dropped.",
  empty: () => "The query returned no data.",
  non_finite: () => "Some values were NaN or infinite and are not drawn (their counts are kept).",
  estimated_counts: () =>
    "Counts are increase() estimates: Prometheus extrapolates within each step, so they are not whole numbers.",
  non_monotonic: () =>
    "Some cumulative bucket counts decreased (independent extrapolation or a reset); the running maximum was used, as histogram_quantile does.",
  missing_inf: () =>
    "The histogram has no +Inf bucket: observations above the largest bucket are missing and n is a lower bound.",
  histogram_as_lines: () =>
    "This looks like histogram buckets drawn as lines; use query_distribution for a heatmap of counts.",
  overflow: () => "Some observations are above the largest bucket edge; their values are unknown (top strip).",
};

/** Plain-language caveat; unknown keys are shown as-is rather than hidden. */
export const caveatText = (key: string, nMin: number | null = null, representation = "bucket_agg"): string =>
  (CAVEATS[key] ?? (() => key))(nMin, representation === "distribution");

/** Warnings and notes about the graph, kept apart from what the graph displays. */
export function panelNotes(
  caveats: string[],
  opts: {
    yScaledToData: boolean;
    nMin: number | null;
    representation?: string;
    yView?: { label: string; reason: string | null; author: string; refused: string | null } | null;
  },
): Note[] {
  const notes: Note[] = caveats.map((key) => ({
    kind: "caveat",
    key,
    text: caveatText(key, opts.nMin, opts.representation),
  }));
  const yv = opts.yView;
  if (yv?.refused) {
    notes.push({ kind: "caveat", key: "y_view_refused", text: `The y view ${yv.refused}; showing the automatic range.` });
  } else if (yv) {
    const by = yv.author === "claude" ? " (suggested by Claude)" : "";
    notes.push({ kind: "info", key: "y_view", text: `Y view "${yv.label}"${by}${yv.reason ? `: ${yv.reason}` : ""}` });
    return notes;
  }
  if (opts.yScaledToData && opts.representation !== "distribution") {
    notes.push({ kind: "info", key: "y_scaled_to_data", text: "The y-axis is scaled to the data (no reference range yet)." });
  }
  return notes;
}

/** One sentence on what the plotted lines are. */
export function describeShown(
  d: Pick<DatasetMeta, "representation" | "quantile"> & { scheme?: DatasetMeta["scheme"] },
  step: string,
  kind = "time",
  mark = "",
): string {
  if (mark === "percentiles") {
    return `Per ${step} column, the source bucket holding each percentile (never interpolated), only where the column has n ≥ 10/(1−q).`;
  }
  if (mark === "quantile_curve") {
    return "Value at each quantile, as source-bucket boxes [F(lo), F(hi)]; the dashed line marks where n stops supporting a quantile (faded beyond it).";
  }
  if (mark === "ccdf") {
    return "Share of observations above each value (log-log), exact at bucket edges and bounded inside a bucket; hover reads a threshold, click pins it.";
  }
  if (kind === "histogram") {
    return `Share of observations per value bucket, summed over each selected window (whole ${step} steps); bars are the source buckets (${d.scheme?.description ?? "unknown scheme"}).`;
  }
  if (d.representation === "distribution") {
    return `Counts per ${step} column and value bucket (colour), from increase() of the histogram; bins are the source buckets (${d.scheme?.description ?? "unknown scheme"}).`;
  }
  if (d.representation === "quantile") {
    const q = d.quantile != null ? `p${Number((d.quantile * 100).toFixed(2))}` : "Percentile";
    return `${q} per ${step} window, computed from the histogram at each step and never aggregated.`;
  }
  return `Average per ${step} bucket (line) with its min–max envelope (band).`;
}
