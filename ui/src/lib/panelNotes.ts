import { provenance } from "../chart/overlays";
import type { Caveat, DatasetMeta, Where, YContext } from "./api";
import { fmtValue } from "../chart/axis";
import { caveatSource, type VariationSource } from "./sources";

export interface Note {
  kind: "caveat" | "info";
  key: string;
  text: string;
  where?: Where | null;
  /** spec §5.4: the caveat is about the instruments (measurement system), not the process */
  source?: VariationSource | null;
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
  fake_resolution: () => "The query step is finer than the series interval (how often the series is sampled; for scraped metrics, the scrape interval): the extra detail is not real.",
  partial: () => "Some incomplete source cells were dropped.",
  empty: () => "The query returned no data.",
  non_finite: () => "Some values were NaN or infinite and are not drawn (their counts are kept).",
  no_value: () =>
    "Some buckets had samples but the query gave no value for them; they are not drawn (their counts are kept). The cause is unknown.",
  estimated_counts: () =>
    "Counts are increase() estimates: Prometheus extrapolates within each query bucket, so they are not whole numbers.",
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
  absent_as_zero: () =>
    "A counter series that only exists after its first event (e.g. an error status) was read as 0 where it was absent and its live sibling (same instrument, another outcome) reported: an assumption about the instrument. Gaps inside the series' lifetime, and steps where the sibling is absent too, stay gaps.",
  cycles_excluded: () => "Some previous cycles were left out of the reference (missing data, excluded dates, or atypical); see the legend.",
  heavy_tails: () => "Previous cycles had excursions beyond the normal-theory threshold, so the extreme-point threshold was raised to the largest of them.",
  small_residual_pool: () => "Few previous-cycle residuals: the band edges are rough.",
  dst_wall_clock: () => "The time range or a previous cycle crosses a daylight-saving change: points are matched by local wall-clock time, so a skipped hour has no comparison and a repeated hour is compared with the same reference hour.",
  missing_data: () => "Some series have buckets with no or too few samples (see the coverage rug).",
  untrusted_data: () => "Part of the time range could not be fetched or judged; it is hatched.",
  post_gap_spike: () => "A value right after a gap is computed from the sample before the gap (VictoriaMetrics): increase/delta include the whole gap's change, idelta returns the raw sample. Not a real spike.",
  interval_differs: () => "Some series are sampled at a different rate than the source is configured for. Coverage is judged against each series' own rate, so loss lasting most of the time range cannot show.",
  interval_change: () => "A series' sample rate changed within the time range: buckets at the other rate may read ok or partial.",
  unobservable_counts: () => "This expression's sample counts cannot be observed (a subquery fills gaps), so coverage is unknown, not zero. Split the expression to check coverage.",
  member_coverage_unknown: () => "Aggregated at the source: missing member series cannot be seen.",
  heavy_tailed_noise: () => "The members' noise has heavier tails than normal: spike and short-episode thresholds follow the other members' own peaks, so only excursions unusual for this fleet are named.",
  many_outliers: () => "More than 10% of the members were named: they differ systematically (sizes, roles, zones). Compare shapes with normalise=\"member\" or split the fleet by a label.",
  clustered: () => "The members form behaviour groups (e.g. sizes or roles): each group was analysed as its own fleet, and outliers are named against their own group. In the SPC band view each group has its own median ± 2σ/3σ zones (σ from its own tests); in the quantile view its 25–75% band and median, inside the whole fleet's min–max.",
  members_skipped: () => "Some members had too little data to be tested; they are in the band but not judged.",
  constant_reference: () => "Every other member reports one value at every step (e.g. exactly 0 errors): the members that depart from it are named in the summary but not judged, since the others' spread is zero and no noise scale exists to test them against.",
  no_spread: () => "Every member reports the same value at every step: there is no spread and no outlier.",
  degenerate_excursion_scale: () => "Most member-steps are exact ties (e.g. sparse counters at 0), so the episode thresholds use the rolling median's scale for independent normal noise instead of the fleet's own; ties make that conservative.",
  too_few_members_for_outliers: () => "Fewer than 10 members had enough data: the band is shown, no member is judged.",
  members_partial: () => "Many member-steps rest on fewer samples than the members usually report: those values are noisier, and an excursion there may be a collection artefact.",
  members_missing: () => "Some members did not report at some steps: the band is over the members that did (n per step), never imputed; the strip at the bottom marks those steps.",
  short_baseline: () => "The SPC baseline is short (n_eff < 100): its limits are rough; the darker strips show how rough, and p-values allow for it.",
  near_random_walk: () => "The series is close to a random walk (lag-1 φ > 0.9): the residual chart is slow to see sustained shifts.",
  seasonal_not_in_baseline: () => "A cycle in the series is longer than half the SPC baseline and no operating profile models it: the centre line ignores it.",
  nonmergeable_aggregation: () =>
    "An already-computed percentile was averaged, summed or merged over time or series on request: this is badly wrong in practice (24 hourly p90s averaged to 60.3 ms; the true p90 of the merged 811k requests was 35.8 ms, a 68.5% error). Recompute it from the merged histogram or raw data.",
  overflow: () => "Some observations are above the largest bucket edge; their values are unknown (top strip).",
  no_uncertainty: () => "Produced by code without a declared uncertainty (or exact): its uncertainty is unknown, not zero. It can be cited, but a finding that does is marked \"uncertainty unknown\".",
  input_uncertainty_unknown: () => "The interval covers this step only: an input's uncertainty is unknown, so the true error can be larger (a lower bound).",
  uncertainty_not_propagated: () => "The interval leaves out the inputs' own declared intervals (the code did not say it propagated them): a lower bound on the error.",
  counts_unknown: () => "The code gave no sample counts: coverage is unknown (not zero) and a coarser view averages the bucket values unweighted.",
  failed_spans: () => "An input of the code had spans the source could not return.",
  series_cut: () => "More series than a line chart draws: the most outstanding are lines, the rest one grey 'others' band (their median and min–max); none is dropped.",
  zero_is_no_documents: () =>
    "A query bucket with no matching documents reads as 0 per second: Elasticsearch cannot tell no traffic from documents that were never ingested.",
  approximate_percentile: () =>
    "Elasticsearch/OpenSearch percentiles are TDigest estimates (approximate), not exact order statistics.",
  query_chosen_buckets: () =>
    "The value buckets were chosen by the query (its interval), not by the source: re-query with a smaller interval for more detail.",
};

/** Plain-language caveat; unknown keys are shown as-is rather than hidden. */
const SOURCE_WARNING = "source_warning:";

export const caveatText = (key: string, nMin: number | null = null, representation = "bucket_agg"): string =>
  key.startsWith(SOURCE_WARNING)
    ? `Source note: ${key.slice(SOURCE_WARNING.length)}`
    : (CAVEATS[key] ?? (() => key))(nMin, representation === "distribution");

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
      kind: key.startsWith(SOURCE_WARNING) ? "info" : "caveat",
      key,
      text: caveatText(key, opts.nMin, opts.representation),
      ...(caveatSource(key) ? { source: caveatSource(key) } : {}),
    }));
  (opts.located ?? []).forEach((c, i) =>
    notes.push({
      kind: c.severity === "info" ? "info" : "caveat", key: `${c.code}:${i}`, text: c.message, where: c.where ?? null,
      ...(caveatSource(c.code) ? { source: caveatSource(c.code) } : {}),
    }),
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
      notes.push({ kind: "caveat", key: "marginal_low_n", text: `The marginal has fewer than ${mg.nMin} values in a time range; its shape is noise (drawn faded).` });
    }
  }
  if (opts.auto?.transform === "reframe") {
    notes.push({
      kind: "caveat", key: "auto_reframe",
      text: `Reframed, not the metric as asked: ${opts.auto.reason}. The original is dataset ${opts.auto.source_dataset}.`,
    });
  }
  if (opts.auto?.transform === "rescope") {
    notes.push({
      kind: "info", key: "auto_rescope",
      text: `Over a different time range than the panel it came from: ${opts.auto.reason}. That panel's data is dataset ${opts.auto.source_dataset}, unchanged.`,
    });
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
  const limits = (c.lines ?? []).filter((l) => (l.kind ?? "limit") === "limit");
  for (const l of limits.length ? limits : c.limit ? [c.limit] : []) parts.push(`physical limit ${l.label ?? l.metric} (${fmtValue(l.hi, unit)})`);
  if (parts.length) out.push({ kind: "info", key: "y_reference", text: `The y range is the reference range: it includes the ${parts.join(" and the ")}, so small changes look small.` });
  (c.lines ?? []).forEach((l, i) => {
    const who = `origin: ${provenance(l.origin, l.confidence) || "unknown"}`;
    const what = l.kind === "threshold" ? "threshold" : l.kind === "reference" ? "reference" : "limit";
    const at = l.value != null || l.kind !== "reference" ? ` at ${fmtValue(l.hi, unit)}` : "";
    out.push({ kind: "info", key: `context_line_${i}`, text: `${l.label ?? l.metric} (${what})${at}: ${who}; ${l.basis}.` });
  });
  for (const n of c.notes) {
    const i = n.indexOf(": ");
    const key = i < 0 ? n : n.slice(0, i), text = i < 0 ? "" : n.slice(i + 2);
    if (key === "profile_pending") out.push({ kind: "info", key, text: "The normal range is still being computed; it will join the y range shortly." });
    else if (key === "profile_unavailable") out.push({ kind: "info", key, text: `No normal range yet: ${text}` });
    else if (key === "context_unavailable") out.push({ kind: "caveat", key, text: `Context line not drawn: ${text}` });
    else if (key === "limit_unavailable") out.push({ kind: "caveat", key, text: `Physical limit not applied: ${text}` });
    // natural_bounds_unknown is the common case for derived expressions: not worth a note
  }
  return out;
}

/** "declared 95% confidence interval (bootstrap)" for a code output's band, or null. */
export function intervalLegend(d: Pick<DatasetMeta, "uncertainty">): string | null {
  const u = d.uncertainty;
  if (!u || u.exact) return null;
  const level = u.level != null ? `${Number((u.level * 100).toPrecision(3))}% ` : "";
  return `declared ${level}${u.kind ?? "confidence"} interval (${u.method ?? "method not stated"})`;
}

/** Where the panel's data came from: the source, or the code node and its inputs (spec §5.2). */
export function provenanceText(d: Pick<DatasetMeta, "source" | "producer" | "parents">): string {
  const p = d.producer;
  const from = d.parents?.length ? ` from ${d.parents.join(", ")}` : "";
  if (p?.kind === "binding") return `${d.source}: ${p.description ?? "derived"}${from}`;
  if (!p || p.kind !== "code") return d.source;
  return `produced by code node ${p.node} (output ${p.output})${from}`;
}

/** provenanceText split around the code node id so the UI can make it a button; null for a source. */
export function provenanceParts(d: Pick<DatasetMeta, "source" | "producer" | "parents">): [string, string, string] | null {
  const p = d.producer;
  if (!p || p.kind !== "code" || !p.node) return null;
  const text = provenanceText(d);
  const at = text.indexOf(p.node);
  return [text.slice(0, at), p.node, text.slice(at + p.node.length)];
}

/** Elasticsearch histogram buckets: chosen by the query, edges [lo, hi). */
const isLinear = (d: { scheme?: DatasetMeta["scheme"] }): boolean => d.scheme?.kind === "linear";

/** One sentence on what the plotted lines are. */
export function describeShown(
  d: Pick<DatasetMeta, "representation" | "quantile"> & {
    scheme?: DatasetMeta["scheme"]; histogram?: DatasetMeta["histogram"];
    producer?: DatasetMeta["producer"]; uncertainty?: DatasetMeta["uncertainty"];
  },
  step: string,
  kind = "time",
  mark = "",
  fleetView = "band",
  fleetGrouped = false,
): string {
  if (kind === "spectrum") return "Periodogram (Lomb-Scargle): the share of variance a sinusoid of each period explains, with the 1% false-alarm level; peaks carry intervals.";
  if (kind === "spc") return "Control chart: the series against a centre line and 3σ band computed from the baseline only (shaded, or an earlier time range named below); flagged points break SPC rules.";
  if (kind === "fleet" && fleetView === "heat") return "Member × time: one row per member, one column per step; colour is the member's deviation from the fleet median in robust σ (orange above, purple below, capped), dots where a live member was silent.";
  if (kind === "fleet" && fleetView === "multiples") return "Small multiples: one panel per outlying member on the same y range, its line against the reference band it was judged against.";
  if (kind === "fleet" && fleetGrouped && fleetView === "quantiles") return "Fleet in behaviour groups: the whole fleet's min–max shaded, and per group (members that behave alike) its 25–75% band and median line; outliers, judged within their own group, drawn as lines.";
  if (kind === "fleet" && fleetGrouped) return "Fleet in behaviour groups: per group (members that behave alike) its own reference band and median; outliers, judged within their own group, drawn as lines.";
  if (kind === "fleet" && fleetView === "quantiles") return "Fleet spread: how the members that reported are spread at each step (descriptive), lighter where missing members could move it, and only the outlying members drawn as lines.";
  if (kind === "fleet") return "Fleet: the reference band the outlier tests judge against, and only the outlying members drawn as lines; the key above the plot gives the encoding.";
  if (kind === "littles") return "Little's law check: per window, mean concurrency L (gauge) against throughput × mean latency λ·W, each with a 95% band, and their ratio below (1 = consistent); shaded windows break the law.";
  if (kind === "seasonal") return "Seasonal comparison: now against the same time range in previous cycles (faint), their median (dashed) and a 90% band from the spread across those cycles; dots are points too extreme for any previous cycle.";
  if (kind === "spectrogram") return "Spectrogram: how the periodicity changes over time, one analysis window (segment) per column; the segment length sets the period resolution.";
  if (mark === "percentiles") {
    return `Per ${step} column, the source bucket holding each percentile (estimator: bucket-edge bounds, never interpolated), only where the column has n ≥ 10/(1−q).`;
  }
  if (mark === "quantile_curve") {
    return "Value at each quantile, as source-bucket boxes [F(lo), F(hi)]; the dashed line marks where n stops supporting a quantile (faded beyond it).";
  }
  if (mark === "ccdf") {
    return isLinear(d)
      ? "Share of observations at or above each value, P(X ≥ x) (log-log; the query's buckets are [lo, hi)), exact at bucket edges and bounded inside a bucket; hover reads a threshold, click pins it."
      : "Share of observations above each value (log-log), exact at bucket edges and bounded inside a bucket; hover reads a threshold, click pins it.";
  }
  if (kind === "histogram") {
    const bars = isLinear(d) ? "the query's buckets" : "the source buckets";
    return `Share of observations per value bucket, summed over each selected time range (whole ${step} query buckets); bars are ${bars} (${d.scheme?.description ?? "unknown scheme"}).`;
  }
  if (d.producer?.kind === "binding" && d.producer.op === "error_ratio" && kind === "time" && !mark) {
    const band = intervalLegend(d);
    return `Share of requests that failed per ${step} bucket (errors ÷ requests, line)` + (band ? `; band: ${band}.` : ".");
  }
  const code = d.producer?.kind === "code" ? d.producer : null;
  if (code && kind === "time" && !mark) {
    const band = intervalLegend(d);
    return `Values per ${step} bucket as output by code node ${code.node} (line)` +
      (band ? `; band: ${band}.` : "; band: the bucket min–max where the code gave it.");
  }
  if (code && d.representation === "distribution" && kind === "heatmap") {
    return `Counts per ${step} column and value bucket (colour), as output by code node ${code.node}; bins: ${d.scheme?.description ?? "unknown scheme"}.`;
  }
  if (d.representation === "distribution") {
    return isLinear(d)
      ? `Document counts per ${step} column and value bucket (colour); bins chosen by the query (${d.scheme?.description ?? "unknown scheme"}).`
      : `Counts per ${step} column and value bucket (colour), from increase() of the histogram; bins are the source buckets (${d.scheme?.description ?? "unknown scheme"}).`;
  }
  if (d.representation === "quantile") {
    const q = d.quantile != null ? `p${Number((d.quantile * 100).toFixed(2))}` : "Percentile";
    const est = d.histogram
      ? "estimator: histogram_quantile, linear interpolation within the bucket holding q (the source's definition)"
      : "estimator: as computed by the source (e.g. quantile_over_time or a summary's own quantile)";
    return `${q} at each ${step} query step (over the query window), never aggregated; ${est}.`;
  }
  return `Average per ${step} bucket (line) with its min–max envelope (band).`;
}
