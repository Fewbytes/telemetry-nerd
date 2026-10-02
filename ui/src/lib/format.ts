import type { EvidenceRef, Scope } from "./api";

/** HH:MM UTC — the spec renders times like "14:00–14:30". */
export const fmtTime = (ms: number): string => new Date(ms).toISOString().slice(11, 16);

const day = (ms: number): string => new Date(ms).toISOString().slice(0, 10);

/** Time of day, prefixed with the UTC date ("09-30 23:59") — for ranges that cross midnight. */
const fmtTimeDated = (ms: number): string => new Date(ms).toISOString().slice(5, 16).replace("T", " ");

/** "HH:MM–HH:MM" UTC; each end carries its date when the range crosses midnight. */
export const fmtRange = (startMs: number, endMs: number): string =>
  day(startMs) === day(endMs)
    ? `${fmtTime(startMs)}–${fmtTime(endMs)}`
    : `${fmtTimeDated(startMs)}–${fmtTimeDated(endMs)}`;

export type StatisticRef = Extract<EvidenceRef, { kind: "statistic" }>;

/**
 * Spec §2 rendered scope: "<selector> · <start>–<end> UTC · <step> step · <aggregation>",
 * plus " · vs <baseline>" when a baseline range is present.
 */
export function scopeLine(scope: Scope): string {
  const range = fmtRange(scope.time_range.start_ms, scope.time_range.end_ms);
  let line = `${scope.selector} · ${range} UTC · ${scope.step} step · ${scope.aggregation}`;
  if (scope.baseline_range) {
    line += ` · vs ${fmtRange(scope.baseline_range.start_ms, scope.baseline_range.end_ms)}`;
  }
  return line;
}

/**
 * Statistic evidence line: "name = value [lo, hi] (method)" with an interval,
 * or "name = value (exact, method)" when the value is exact.
 */
export function statLine(ref: StatisticRef): string {
  if (ref.exact) return `${ref.name} = ${ref.value} (exact, ${ref.method})`;
  // no interval and not exact: unknown, never shown as if exact (spec §5.3)
  if (ref.interval === null) return `${ref.name} = ${ref.value} (uncertainty unknown, ${ref.method})`;
  return `${ref.name} = ${ref.value} [${ref.interval[0]}, ${ref.interval[1]}] (${ref.method})`;
}

const FLAG_LABELS: Record<string, string> = {
  uncertainty_unknown: "uncertainty unknown",
  input_uncertainty_unknown: "input uncertainty unknown",
  uncertainty_not_propagated: "uncertainty not propagated",
};

/** Short chip text for an evidence uncertainty flag (spec §5.3). */
export const flagLabel = (flag: string): string => FLAG_LABELS[flag] ?? flag.replaceAll("_", " ");

/** Short label for any evidence ref; statistic refs render via statLine. */
export function refLabel(ref: EvidenceRef): string {
  if (ref.kind === "panel") return `panel ${ref.panel}`;
  if (ref.kind === "annotation") return `annotation ${ref.annotation}`;
  if (ref.kind === "claim") return `${ref.metric} ${ref.field}: ${ref.origins.join(" vs ")} disagree`;
  return statLine(ref);
}

/** Step length as "2m" (whole minutes) or "30s". */
export const fmtStep = (ms: number): string => (ms % 60_000 === 0 ? `${ms / 60_000}m` : `${ms / 1000}s`);

/** HH:MMZ, for fleet times that are always UTC. */
export const fmtTimeZ = (ms: number): string => `${fmtTime(ms)}Z`;

const sig = (v: number): string => String(Number(v.toPrecision(3)));

// decimal SI prefixes, largest first so the first match wins
const SI_PREFIXES: [number, string][] = [[1e12, "T"], [1e9, "G"], [1e6, "M"], [1e3, "k"]];

// units a catalog can carry that never take a magnitude prefix (bead 2as "catalog" UNITS set)
const UNSCALED_UNITS = new Set(["%", "ratio", "°C"]);

/**
 * SI-scaled, unit-aware value label for a y-axis tick: "1.4 GB/s", "320 ms", "97%". The one
 * formatter every unit-carrying axis (time panels, seasonal, heatmap/distribution) should share,
 * so a counter in the billions reads as "1.4 G..." instead of a raw, comma-grouped integer.
 */
export function fmtSI(v: number, unit: string | null): string {
  if (unit === "%") return `${sig(v)}%`;
  if (!unit || UNSCALED_UNITS.has(unit)) return unit ? `${sig(v)} ${unit}` : sig(v);
  if (unit === "s" && v !== 0 && Math.abs(v) < 1) return `${sig(v * 1000)} ms`;
  const abs = Math.abs(v);
  const hit = SI_PREFIXES.find(([mag]) => abs >= mag);
  return hit ? `${sig(v / hit[0])} ${hit[1]}${unit}` : `${sig(v)} ${unit}`;
}
