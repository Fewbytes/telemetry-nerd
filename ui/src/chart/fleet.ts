import type uPlot from "uplot";

/** Fleet panel (bead lkn.3): many series of one metric as a group band + outlying members. */
export interface FleetOutlier {
  id: string; labels: Record<string, string>; kind: "persistent" | "drifting" | "shifted" | "transient";
  direction: "higher" | "lower"; score: number | null; values: (number | null)[];
  since_ms: number | null; episodes: [number, number][];
}
export interface FleetBand {
  median: (number | null)[]; q25: (number | null)[]; q75: (number | null)[];
  q10: (number | null)[]; q90: (number | null)[]; lo: (number | null)[]; hi: (number | null)[];
}
export interface FleetData {
  ts: number[]; members: number; normalise: "none" | "member"; scale: "log" | "linear";
  band: FleetBand; n: number[]; alive: number[]; outliers: FleetOutlier[]; outlier_count: number;
}

/** Okabe-Ito, colour-blind safe; the band is grey, so outliers keep the colours. */
export const OUTLIER_COLORS = ["#D55E00", "#0072B2", "#CC79A7", "#009E73", "#E69F00", "#56B4E9"];

/**
 * Columns: x (s), min, max, q10, q90, q25, q75, median, then one per drawn outlier.
 * Bands are nested fills (spread as intensity, no boundary lines): max-min, q90-q10, q75-q25.
 * Nulls stay nulls: a step with too few members has no band there, never interpolated.
 */
export function toFleetUplot(d: FleetData): { data: uPlot.AlignedData; bands: uPlot.Band[]; roles: string[] } {
  const x = d.ts.map((t) => t / 1000);
  const b = d.band;
  const cols = [x, b.lo, b.hi, b.q10, b.q90, b.q25, b.q75, b.median, ...d.outliers.map((o) => o.values)];
  const roles = ["x", "lo", "hi", "q10", "q90", "q25", "q75", "median", ...d.outliers.map(() => "outlier")];
  return { data: cols as uPlot.AlignedData, bands: [{ series: [2, 1] }, { series: [4, 3] }, { series: [6, 5] }], roles };
}

const KIND_TEXT: Record<FleetOutlier["kind"], string> = {
  persistent: "consistently", drifting: "drifting", shifted: "shifted", transient: "briefly",
};

export function outlierText(o: FleetOutlier, fmtTime: (ms: number) => string): string {
  const since = o.kind === "transient"
    ? o.episodes.length ? ` · ${o.episodes.length === 1 ? "episode" : `${o.episodes.length} episodes`} from ${fmtTime(o.episodes[0][0])}` : ""
    : o.since_ms !== null ? ` since ${fmtTime(o.since_ms)}` : "";
  return `${o.id}: ${KIND_TEXT[o.kind]} ${o.direction}${since}`;
}

export function fleetLegend(d: FleetData): string {
  const shown = d.outliers.length;
  const more = d.outlier_count - shown;
  const nMin = Math.min(...d.n), nMax = Math.max(...d.n);
  const n = nMin === nMax ? `${nMax} reporting per step` : `${nMin}–${nMax} reporting per step`;
  const units = d.normalise === "member" ? " · each member relative to its own median" : "";
  const out = d.outlier_count === 0 ? "no outliers" : `${d.outlier_count} outlier${d.outlier_count > 1 ? "s" : ""}${more > 0 ? ` (${shown} drawn)` : ""}`;
  return `${d.members} members · ${out} · ${n} · shading: min–max, 10–90%, 25–75% across reporting members · line: median${units}`;
}

/** Steps where fewer members reported than were alive: the coverage strip's cells. */
export function coverageGaps(d: FleetData): { x: number; share: number }[] {
  return d.ts.flatMap((t, i) => (d.alive[i] > 0 && d.n[i] < d.alive[i] ? [{ x: t / 1000, share: 1 - d.n[i] / d.alive[i] }] : []));
}
