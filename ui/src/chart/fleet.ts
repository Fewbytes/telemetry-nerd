import type uPlot from "uplot";
import type { YContext, YView } from "../lib/api";
import { resolveY, yStats, type YResolved } from "./yview";

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
export interface FleetHeatRow { id: string; z: (number | null)[]; rank: number | null; first: number; last: number }
/** Member x time matrix of z (bead lkn.11): rows already sorted; None = no report. */
export interface FleetHeat { z_cap: number; rows_total: number; rows: FleetHeatRow[] }
export interface FleetData {
  ts: number[]; members: number; normalise: "none" | "member"; scale: "log" | "linear";
  band: FleetBand; n: number[]; alive: number[]; outliers: FleetOutlier[]; outlier_count: number; heat?: FleetHeat;
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

/** Nested band fills, grey so the outliers keep the colours. */
export const bandFills = (dark: boolean): string[] =>
  dark
    ? ["rgba(150,150,150,0.14)", "rgba(150,150,150,0.20)", "rgba(150,150,150,0.30)"]
    : ["rgba(110,110,110,0.10)", "rgba(110,110,110,0.16)", "rgba(110,110,110,0.26)"];

/** The fleet's drawn values as y stats: the spread's min-max and every drawn outlier. */
export function fleetStats(d: FleetData) {
  const vals = (a: (number | null)[]) => a;
  const mk = (id: string, avg: (number | null)[]) => ({ id, labels: {}, ts: d.ts, avg, min: avg, max: avg, count: avg.map(() => 1) });
  const series = [mk("lo", vals(d.band.lo)), mk("hi", vals(d.band.hi)), ...d.outliers.map((o) => mk(o.id, o.values))];
  return yStats(series, { quantile: false, nMin: null });
}

/** The y range of a fleet panel: its metric's natural bounds by default, like a time-series panel.
 *  Natural bounds are in the metric's own units: they do not apply once members are normalised to
 *  their own median. (`scale` is the deviation-analysis scale, not the drawn axis, which is linear:
 *  so no log y view either.) */
export function fleetY(d: FleetData, ctx: YContext | null, chosen: YView | null = null): YResolved & { stats: ReturnType<typeof fleetStats> } {
  const st = fleetStats(d);
  const usable = d.normalise === "member" ? null : ctx;
  return { ...resolveY(chosen, st, usable, true), stats: st };
}
