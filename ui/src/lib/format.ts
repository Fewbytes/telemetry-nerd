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
  if (ref.exact || ref.interval === null) {
    return `${ref.name} = ${ref.value} (exact, ${ref.method})`;
  }
  return `${ref.name} = ${ref.value} [${ref.interval[0]}, ${ref.interval[1]}] (${ref.method})`;
}

/** Short label for any evidence ref; statistic refs render via statLine. */
export function refLabel(ref: EvidenceRef): string {
  if (ref.kind === "panel") return `panel ${ref.panel}`;
  if (ref.kind === "annotation") return `annotation ${ref.annotation}`;
  return statLine(ref);
}
