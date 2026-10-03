/** Sources of variation (docs/principles.md principle 8; mirrors analysis/sources.py). */
export type VariationSource = "common_cause" | "special_cause" | "measurement_system" | "undetermined";

const TEXT: Record<VariationSource, string> = {
  common_cause: "common cause",
  special_cause: "special cause",
  measurement_system: "measurement system",
  undetermined: "source undetermined",
};

export const sourceText = (v: VariationSource | string | null | undefined): string =>
  v ? TEXT[v as VariationSource] ?? v.replaceAll("_", " ") : TEXT.undetermined;

/** Caveat codes that describe the instruments, not the process (analysis.sources.MEASUREMENT_CAVEATS). */
export const MEASUREMENT_CAVEATS = new Set([
  "gaps", "partial", "missing_data", "untrusted_data", "unobservable_counts", "post_gap_spike",
  "interval_change", "interval_differs", "absent_part", "coarsened", "fake_resolution",
  "sampling_artifact", "extrapolated", "resets", "members_missing", "members_skipped",
  "member_coverage_unknown", "members_partial", "skipped_series", "latency_unit_assumed",
  "estimated_counts", "counts_unknown", "n_unknown", "no_uncertainty", "input_uncertainty_unknown",
  "uncertainty_not_propagated",
]);

export const caveatSource = (code: string): VariationSource | null =>
  MEASUREMENT_CAVEATS.has(code) ? "measurement_system" : null;

/** "3 special cause, 12 common cause" for labelled items (most actionable first); "" when none. */
export function sourceCounts(items: { source?: VariationSource | string | null }[]): string {
  const order: VariationSource[] = ["special_cause", "undetermined", "measurement_system", "common_cause"];
  const n = new Map<string, number>();
  for (const it of items) if (it.source) n.set(it.source, (n.get(it.source) ?? 0) + 1);
  return order.filter((s) => n.get(s)).map((s) => `${n.get(s)} ${sourceText(s)}`).join(", ");
}
