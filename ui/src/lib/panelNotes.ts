import type { Caveat, DatasetMeta, Where, YContext } from "./api";
import { fmtValue } from "../chart/axis";

export interface Note {
  kind: "caveat" | "info";
  key: string;
  text: string;
  where?: Where | null;
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
  filtered: () => "Values are filtered, not raw: cite the filter, not the metric.",
  filter_edges: () => "Dashed spans saw a truncated kernel (series start/end, around gaps): unreliable.",
  mostly_edge: () => "Most of this series is within the filter's edge zone: use a shorter cutoff or a longer range.",
  weak_filter: () => "The cutoff is under 8 steps: it barely smooths.",
  coarsened: () => "Averaged to a coarser step first (point cap): shorter periods were not examined.",
  red_noise: () => "The series is autocorrelated: long periods look stronger than white noise would explain, so peaks are only called significant against AR(1) red noise (dotted level).",
  sampling_artifact: () => "A peak matches the sampling pattern (periodic gaps), not the signal.",
  too_few_points: () => "Some series have too few points for a spectrum and were skipped.",
  too_gappy: () => "Some series are more than half gaps and were skipped.",
  constant: () => "Some series are constant and were skipped.",
  skipped_series: () => "Some series did not qualify and were skipped.",
  cycles_excluded: () => "Some previous cycles were left out of the reference (missing data, excluded dates, or atypical); see the legend.",
  heavy_tails: () => "Previous cycles had excursions beyond the normal-theory threshold, so the extreme-point threshold was raised to the largest of them.",
  small_residual_pool: () => "Few previous-cycle residuals: the band edges are rough.",
  dst_within_window: () => "The window crosses a daylight-saving change: points after it are an hour off the local-time alignment.",
  missing_data: () => "Some series have buckets with no or too few samples (see the coverage rug).",
  untrusted_data: () => "Part of the window could not be fetched or judged; it is hatched.",
  member_coverage_unknown: () => "Aggregated at the source: missing member series cannot be seen.",
  heavy_tailed_noise: () => "The members' noise has heavier tails than normal: spike and short-episode thresholds follow the other members' own peaks, so only excursions unusual for this fleet are named.",
  many_outliers: () => "More than 10% of the members were named: they differ systematically (sizes, roles, zones). Compare shapes with normalise=\"member\" or split the fleet by a label.",
  members_skipped: () => "Some members had too little data to be tested; they are in the band but not judged.",
  too_few_members_for_outliers: () => "Fewer than 10 members had enough data: the band is shown, no member is judged.",
  members_missing: () => "Some members did not report at some steps: the band is over the members that did (n per step), never imputed; the strip at the bottom marks those steps.",
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
    auto?: { transform: string; source_dataset: string; reason: string } | null;
    yContext?: YContext | null;
    unit?: string | null;
    nMin: number | null;
    representation?: string;
    yView?: { label: string; reason: string | null; author: string; refused: string | null } | null;
    filter?: { label: string; reason: string } | null;
    indexed?: { label: string; skipped: string[]; hidden: number; nonPositive: number } | null;
    located?: Caveat[];
    marginal?: { what: string; ref: string; n: number[]; nMin: number; author: string; reason: string | null } | null;
  },
): Note[] {
  const locatedCodes = new Set((opts.located ?? []).map((c) => c.code));
  const notes: Note[] = caveats
    .filter((key) => !locatedCodes.has(key))
    .map((key) => ({
      kind: "caveat",
      key,
      text: caveatText(key, opts.nMin, opts.representation),
    }));
  (opts.located ?? []).forEach((c, i) =>
    notes.push({ kind: c.severity === "info" ? "info" : "caveat", key: `${c.code}:${i}`, text: c.message, where: c.where ?? null }),
  );
  if (opts.filter) {
    notes.push({ kind: "info", key: "filter", text: `Filtered: ${opts.filter.label} — ${opts.filter.reason}. Raw is one click away.` });
  }
  const ix = opts.indexed;
  if (ix) {
    notes.push({ kind: "info", key: "indexed", text: `Indexed: ${ix.label}. Log ratio axis, 1 = no change; ×2 and ×0.5 are equally far from 1.` });
    if (ix.skipped.length) {
      notes.push({ kind: "caveat", key: "indexed_skipped", text: `Not indexed (baseline missing or ≤ 0): ${ix.skipped.join(", ")}.` });
    }
    if (ix.hidden + ix.nonPositive > 0) {
      notes.push({ kind: "caveat", key: "indexed_gaps", text: `${ix.hidden} step(s) without a usable baseline and ${ix.nonPositive} value(s) ≤ 0 are not drawn (a ratio needs both > 0 on a log axis).` });
    }
  }
  const mg = opts.marginal;
  if (mg) {
    const by = mg.author === "claude" ? ` Chosen by Claude${mg.reason ? `: ${mg.reason}` : ""}` : "";
    notes.push({
      kind: "info", key: "marginal",
      text: `Marginal (right): ${mg.what}. Filled = now (n=${Math.round(mg.n[0])}), dashed = ${mg.ref} (n=${Math.round(mg.n[1])}).${by}`,
    });
    if (mg.n.some((n) => n < mg.nMin)) {
      notes.push({ kind: "caveat", key: "marginal_low_n", text: `The marginal has fewer than ${mg.nMin} values in a window; its shape is noise (drawn faded).` });
    }
  }
  if (opts.auto?.transform === "rate") {
    notes.push({
      kind: "info", key: "auto_rate",
      text: `Shown as a rate: ${opts.auto.reason}. The running total is dataset ${opts.auto.source_dataset}; ask Claude for it with show(raw=true).`,
    });
  }
  notes.push(...contextNotes(opts.yContext ?? null, opts.unit ?? null));
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

/** What the catalog contributed to the y range, and what it could not (bead 2as.10). */
export function contextNotes(c: YContext | null, unit: string | null = null): Note[] {
  if (!c) return [];
  const out: Note[] = [];
  const parts = [];
  if (c.profile) parts.push(`${c.profile.label} ${fmtValue(c.profile.lo, unit)}–${fmtValue(c.profile.hi, unit)}`);
  if (c.limit) parts.push(`physical limit ${c.limit.metric} (${fmtValue(c.limit.hi, unit)})`);
  if (parts.length) out.push({ kind: "info", key: "y_reference", text: `The y range is the reference range: it includes the ${parts.join(" and the ")}, so small changes look small.` });
  for (const n of c.notes) {
    const i = n.indexOf(": ");
    const key = i < 0 ? n : n.slice(0, i), text = i < 0 ? "" : n.slice(i + 2);
    if (key === "profile_pending") out.push({ kind: "info", key, text: "The normal range is still being computed; it will join the y range shortly." });
    else if (key === "profile_unavailable") out.push({ kind: "info", key, text: `No normal range yet: ${text}` });
    else if (key === "limit_unavailable") out.push({ kind: "caveat", key, text: `Physical limit not applied: ${text}` });
    // natural_bounds_unknown is the common case for derived expressions: not worth a note
  }
  return out;
}

/** One sentence on what the plotted lines are. */
export function describeShown(
  d: Pick<DatasetMeta, "representation" | "quantile"> & { scheme?: DatasetMeta["scheme"]; histogram?: DatasetMeta["histogram"] },
  step: string,
  kind = "time",
  mark = "",
): string {
  if (kind === "spectrum") return "Periodogram (Lomb-Scargle): the share of variance a sinusoid of each period explains, with the 1% false-alarm level; peaks carry intervals.";
  if (kind === "spc") return "Control chart: the series against a centre line and 3σ band computed from the shaded baseline only; flagged points break SPC rules.";
  if (kind === "fleet") return "Fleet: every member of the group, shaded by how many members lie there (min–max, 10–90%, 25–75% at each step, over the members that reported), the median, and only the outlying members drawn as lines.";
  if (kind === "seasonal") return "Seasonal comparison: now against the same window in previous cycles (faint), their median (dashed) and a 90% band from the spread across those cycles; dots are points too extreme for any previous cycle.";
  if (kind === "spectrogram") return "Spectrogram: how the periodicity changes over time, one window per column; the window sets the period resolution.";
  if (mark === "percentiles") {
    return `Per ${step} column, the source bucket holding each percentile (estimator: bucket-edge bounds, never interpolated), only where the column has n ≥ 10/(1−q).`;
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
    const est = d.histogram
      ? "estimator: histogram_quantile, linear interpolation within the bucket holding q (the source's definition)"
      : "estimator: as computed by the source (e.g. quantile_over_time or a summary's own quantile)";
    return `${q} per ${step} window, computed at each step and never aggregated; ${est}.`;
  }
  return `Average per ${step} bucket (line) with its min–max envelope (band).`;
}
