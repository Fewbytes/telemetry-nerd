// ui/src/lib/card.ts — metric card helpers (bead 2as.12). Pure.
import type { CardField, MetricCard } from "./api";

export const ORIGIN_LABEL: Record<string, string> = {
  user: "you", claude: "Claude", stats: "measured", context: "repo/docs", pack: "pack", metadata: "source", rule: "name rule",
};
export const originLabel = (o: string | null): string => (o ? (ORIGIN_LABEL[o] ?? o) : "no claim");

const ENUMS: Record<string, string[]> = {
  type: ["counter", "gauge", "histogram", "summary", "gaugehistogram", "info", "stateset"],
  bounds: ["≥0", "[0,1]", "[0,100]", "none"],
  additivity_series: ["additive", "intensive", "none"],
  additivity_time: ["additive", "intensive", "none"],
};
export type EditControl = { kind: "select"; options: string[] } | { kind: "text" } | null;

/** What to edit a field with: a fixed choice where the catalog only accepts a fixed set. */
export function editControl(f: Pick<CardField, "field" | "editable">): EditControl {
  if (!f.editable) return null;
  return ENUMS[f.field] ? { kind: "select", options: ENUMS[f.field] } : { kind: "text" };
}

/** One line for the collapsed card: the facts a reader wants before opening it. */
export function cardSummary(card: MetricCard): string {
  const m = card.metrics[0];
  if (card.produced_by) return `code output · node ${card.produced_by.node}`;
  if (!m) return card.learned ? "no catalogued metric" : "this source has not been learned";
  const v = (name: string) => m.fields.find((f) => f.field === name)?.value;
  const bits = [v("unit") && `unit ${v("unit")}`, v("type") && String(v("type")), v("bounds") && String(v("bounds"))].filter(Boolean) as string[];
  const conflicts = card.metrics.reduce((n, x) => n + x.fields.filter((f) => f.conflict).length, 0);
  if (conflicts) bits.push(`${conflicts} conflict${conflicts > 1 ? "s" : ""}`);
  if (!bits.length) bits.push("nothing claimed yet");
  return bits.join(" · ");
}

/** A catalog typical_range {lo, hi, n, window, q?}: descriptive, so its quantiles and sample count travel with it. */
const sig4 = (x: unknown): string => (Number.isFinite(Number(x)) ? String(Number(Number(x).toPrecision(4))) : String(x));
const pct = (x: unknown): string => String(Math.round(Number(x) * 1000) / 10);
const fmtRange = (r: Record<string, unknown>): string => {
  const q = Array.isArray(r.q) ? ` (p${pct(r.q[0])}–p${pct(r.q[1])}, n=${r.n}, ${r.window})` : ` (n=${r.n}, ${r.window})`;
  return `${sig4(r.lo)} – ${sig4(r.hi)}${q}`;
};

export const fmtValue = (v: unknown): string => {
  if (v === null || v === undefined) return "—";
  if (!Array.isArray(v)) {
    if (typeof v === "object" && "lo" in v && "hi" in v) return fmtRange(v as Record<string, unknown>);
    return String(v);
  }
  // thresholds are objects: {label, value, tone}
  return v.map((x) => (x && typeof x === "object" && "value" in x ? `${(x as { label?: string }).label ?? "threshold"} = ${(x as { value: number }).value}` : String(x))).join(", ");
};

/** Confirming pins the value that is shown now; editing records what the user typed. */
export const isPinned = (f: CardField): boolean => f.origin === "user";

export const fmtDuration = (ms: number): string => {
  const units: [string, number][] = [["d", 86_400_000], ["h", 3_600_000], ["m", 60_000], ["s", 1000]];
  for (const [u, n] of units) if (ms >= n && ms % n === 0) return `${ms / n}${u}`;
  return `${ms}ms`;
};

export const qualityRows = (q: MetricCard["quality"]): { label: string; value: string; note?: string }[] => [
  { label: "query step", value: fmtDuration(q.step_ms) },
  { label: "series interval (source)", value: fmtDuration(q.resolution_ms) },
  q.scrape_interval_ms
    ? { label: "series interval (measured)", value: fmtDuration(q.scrape_interval_ms), note: q.scrape_interval_ms > q.step_ms ? "coarser than the query step" : undefined }
    : { label: "series interval (measured)", value: "unknown", note: q.scrape_interval_reason ?? undefined },
  { label: "series in this panel", value: String(q.series) },
  { label: "empty buckets", value: q.gap_pct === null ? "unknown" : `${(q.gap_pct * 100).toFixed(1)}%` },
  q.resets.measured && q.resets.resets !== undefined
    ? {
        label: "counter resets",
        value: `${q.resets.resets} resets, ${q.resets.small_decreases} small decreases`,
        note: `${q.resets.verdict} over ${fmtDuration(q.resets.window_ms ?? 0)}, ${q.resets.series} series${q.resets.negatives ? `, ${q.resets.negatives} negative samples` : ""}`,
      }
    : { label: "counter resets", value: "not measured", note: q.resets.reason },
  { label: "cardinality (catalog)", value: "not measured", note: "the source's own series count per metric is not stored yet" },
];
