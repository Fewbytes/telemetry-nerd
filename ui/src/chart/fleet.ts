import type uPlot from "uplot";
import type { Caveat, YContext, YView } from "../lib/api";
import { resolveY, yStats, type YResolved } from "./yview";

/** Fleet panel (bead lkn.3): many series of one metric as a group band + outlying members. */
export interface FleetOutlier {
  id: string; labels: Record<string, string>; kind: "persistent" | "drifting" | "shifted" | "transient";
  direction: "higher" | "lower"; score: number | null; values: (number | null)[];
  since_ms: number | null; episodes: [number, number][];
  /** behaviour group the member was judged in (lkn.10), when the fleet is split */
  cluster?: string;
}
/** A behaviour group (lkn.10): its own median and interquartile band per step. */
export interface FleetCluster {
  id: string; size: number; band: { median: (number | null)[]; q25: (number | null)[]; q75: (number | null)[] };
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
  clusters?: FleetCluster[]; located?: Caveat[];
}

/** The fleet is split into behaviour groups: the whole-fleet quantile band would be bimodal (its
 *  median can sit between the groups, where no member is), so each group is drawn instead. */
export const grouped = (d: FleetData): boolean => (d.clusters?.length ?? 0) >= 2;

/** One style per behaviour group: muted hues kept apart from the Okabe-Ito outlier colours, each
 *  with its own dash so groups stay distinct without colour; line clears 3:1 on the background. */
export const GROUP_STYLES = {
  light: [
    { line: "#14505B", fill: "#2A7F8E", dash: [] as number[] },
    { line: "#4B3D78", fill: "#7A6BB0", dash: [7, 3] },
    { line: "#5C4A12", fill: "#9C8236", dash: [2, 3] },
    { line: "#3B4A59", fill: "#71879C", dash: [9, 3, 2, 3] },
    { line: "#5A2E3E", fill: "#9E6378", dash: [4, 4] },
    { line: "#2F4F2F", fill: "#6E8F6E", dash: [1, 2] },
  ],
  dark: [
    { line: "#BDEAF2", fill: "#5FB7C6", dash: [] as number[] },
    { line: "#D6CCF5", fill: "#A898E0", dash: [7, 3] },
    { line: "#EBDCA8", fill: "#C9AE5E", dash: [2, 3] },
    { line: "#D3DEE8", fill: "#9BB0C4", dash: [9, 3, 2, 3] },
    { line: "#F0C9D6", fill: "#C98DA2", dash: [4, 4] },
    { line: "#CDE6CD", fill: "#93B893", dash: [1, 2] },
  ],
} as const;
export const GROUP_ALPHA = { light: 0.3, dark: 0.32 } as const;
export const groupStyle = (dark: boolean, k: number) => GROUP_STYLES[dark ? "dark" : "light"][k % GROUP_STYLES.light.length];

/** Time spans (ms) where the data is unknown (located `untrusted_data`): hatched, never read as values. */
export function untrustedSpans(d: FleetData): [number, number][] {
  return (d.located ?? []).filter((c) => c.code === "untrusted_data").flatMap((c) => c.where?.spans ?? []);
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
  if (grouped(d)) {
    // grouped (oyi): the whole fleet's min-max for context, then per group q25, q75, median
    const cs = d.clusters!;
    const cols = [x, b.lo, b.hi, ...cs.flatMap((c) => [c.band.q25, c.band.q75, c.band.median]), ...d.outliers.map((o) => o.values)];
    const roles = ["x", "lo", "hi", ...cs.flatMap(() => ["cq25", "cq75", "cmedian"]), ...d.outliers.map(() => "outlier")];
    const bands = [{ series: [2, 1] as [number, number] }, ...cs.map((_, k) => ({ series: [4 + 3 * k, 3 + 3 * k] as [number, number] }))];
    return { data: cols as uPlot.AlignedData, bands, roles };
  }
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
  const group = o.cluster ? ` within group ${o.cluster}` : "";
  return `${o.id}: ${KIND_TEXT[o.kind]} ${o.direction}${group}${since}`;
}

export function fleetLegend(d: FleetData): string {
  const shown = d.outliers.length;
  const more = d.outlier_count - shown;
  const nMin = Math.min(...d.n), nMax = Math.max(...d.n);
  const n = nMin === nMax ? `${nMax} reporting per step` : `${nMin}–${nMax} reporting per step`;
  const units = d.normalise === "member" ? " · each member relative to its own median" : "";
  const out = d.outlier_count === 0 ? "no outliers" : `${d.outlier_count} outlier${d.outlier_count > 1 ? "s" : ""}${more > 0 ? ` (${shown} drawn)` : ""}`;
  if (grouped(d)) {
    const gs = d.clusters!.map((c) => `${c.id} (${c.size})`).join(", ");
    return `${d.members} members in ${d.clusters!.length} behaviour groups: ${gs} · ${out}, each judged within its group · ${n} · shading: min–max across all members; per group 25–75% and its median${units}`;
  }
  return `${d.members} members · ${out} · ${n} · shading: min–max, 10–90%, 25–75% across reporting members · line: median${units}`;
}

/** Steps where fewer members reported than were alive: the coverage strip's cells. */
export function coverageGaps(d: FleetData): { x: number; share: number }[] {
  return d.ts.flatMap((t, i) => (d.alive[i] > 0 && d.n[i] < d.alive[i] ? [{ x: t / 1000, share: 1 - d.n[i] / d.alive[i] }] : []));
}

/** One neutral teal hue for the whole fleet spread (not a series colour, not an outlier colour, not a
 *  viridis/cividis ramp: the heatmap views own those). `line` carries the median and clears 3:1 on
 *  the theme background; the ribbons are the same hue at rising opacity, so the spread reads as
 *  nested ribbons, never as grid cells. */
export const FLEET_HUE = {
  light: { fill: "#2A7F8E", line: "#14505B" },
  dark: { fill: "#5FB7C6", line: "#BDEAF2" },
} as const;
/** Opacity of the min-max, 10-90 and 25-75 ribbons: lightest outermost, darkest innermost. */
export const BAND_ALPHAS = { light: [0.12, 0.24, 0.42], dark: [0.16, 0.30, 0.50] } as const;

const rgba = (hex: string, a: number): string => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
};

/** Nested ribbon fills in the single fleet hue, outermost (min-max) first. */
export const bandFills = (dark: boolean): string[] => {
  const m = dark ? "dark" : "light";
  return BAND_ALPHAS[m].map((a) => rgba(FLEET_HUE[m].fill, a));
};

/** Legend entries for the encoding row: three ribbons, the median line and the outlier marker. */
/** `line`: the group's median colour and CSS border style (its dash), drawn across the swatch. */
export interface KeyEntry { id: string; label: string; swatch: string; line?: { color: string; style: "solid" | "dashed" | "dotted" }; hatch?: boolean }
const cssDash = (dash: readonly number[]): "solid" | "dashed" | "dotted" => (!dash.length ? "solid" : dash[0] <= 2 ? "dotted" : "dashed");
export function fleetKey(dark: boolean, d?: FleetData): KeyEntry[] {
  const m = dark ? "dark" : "light";
  const f = bandFills(dark);
  const unknown: KeyEntry[] = d && untrustedSpans(d).length
    ? [{ id: "unknown", label: "hatched: data unknown", swatch: dark ? "#9aa0a6" : "#6b7075", hatch: true }]
    : [];
  if (d && grouped(d)) {
    return [
      { id: "minmax", label: "min–max (all)", swatch: f[0] },
      ...d.clusters!.map((c, k) => {
        const g = groupStyle(dark, k);
        return {
          id: `group-${c.id}`, label: `group ${c.id} (${c.size}): 25–75% + median`, swatch: rgba(g.fill, GROUP_ALPHA[m]),
          line: { color: g.line, style: cssDash(g.dash) },
        };
      }),
      { id: "outlier", label: "outlier member (hover for id)", swatch: OUTLIER_COLORS[0] },
      ...unknown,
    ];
  }
  return [
    { id: "minmax", label: "min–max", swatch: f[0] },
    { id: "q1090", label: "10–90%", swatch: f[1] },
    { id: "q2575", label: "25–75%", swatch: f[2] },
    { id: "median", label: "median", swatch: FLEET_HUE[m].line },
    { id: "outlier", label: "outlier member (hover for id)", swatch: OUTLIER_COLORS[0] },
    ...unknown,
  ];
}

/** Names the chart type and its encoding, so it is legible without reading the long legend. */
export function fleetAxisLabel(d: FleetData, unit: string | null): string {
  const u = d.normalise === "member" ? "× own median" : unit || "value";
  if (grouped(d)) return `${u} · ${d.members} members in ${d.clusters!.length} behaviour groups (per group: 25–75 band + median; all: min–max)`;
  return `${u} · spread across ${d.members} members (bands: 25–75, 10–90, min–max)`;
}

/** Steps where an outlier sample is isolated (null on both sides): a line cannot show it, so it gets a dot.
 *  Every other sample is part of the drawn line; markers appear only on hover. */
export function outlierMarkIdx(d: FleetData, k: number): number[] {
  const o = d.outliers[k];
  if (!o) return [];
  const v = o.values;
  return v.flatMap((x, i) => (x !== null && (i === 0 || v[i - 1] === null) && (i === v.length - 1 || v[i + 1] === null) ? [i] : []));
}

/** The outlier whose marker is nearest `yVal` at step `idx` (within `tol` in y units), for the hover label. */
export function nearestOutlier(d: FleetData, idx: number, yVal: number, tol: number): FleetOutlier | null {
  let best: FleetOutlier | null = null, bd = tol;
  for (const o of d.outliers) {
    const v = o.values[idx];
    if (v === null || v === undefined) continue;
    const dist = Math.abs(v - yVal);
    if (dist <= bd) { bd = dist; best = o; }
  }
  return best;
}

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

/** An end label: anchored at its line's last point; `right` is the label's right edge (it is drawn
 *  right-aligned, left of the point), `w` its width, `y` the point's y (all px). */
export interface EndLabel { right: number; w: number; y: number }

/** Vertical label positions (centres) so no two labels whose boxes share x overlap (bead cis):
 *  labels are grouped by horizontal overlap (transitively); within a group, sorted by y, each is
 *  pushed down to clear the one above by `h`, then the group is pushed back up from `bottom`, and
 *  finally clamped to `top`. Order is preserved, so a label never jumps over its neighbour. */
export function placeEndLabels(items: EndLabel[], h: number, top: number, bottom: number): number[] {
  const out = items.map((it) => Math.min(Math.max(it.y, top + h / 2), bottom - h / 2));
  const order = items.map((_, i) => i).sort((a, b) => items[a].right - items[a].w - (items[b].right - items[b].w));
  // horizontal groups: sweep by left edge, a label joins the group while it overlaps the group's extent
  const groups: number[][] = [];
  let reach = -Infinity;
  for (const i of order) {
    const l = items[i].right - items[i].w;
    if (groups.length && l <= reach) {
      groups[groups.length - 1].push(i);
      reach = Math.max(reach, items[i].right);
    } else {
      groups.push([i]);
      reach = items[i].right;
    }
  }
  for (const g of groups) {
    if (g.length < 2) continue;
    g.sort((a, b) => out[a] - out[b] || a - b);
    for (let k = 1; k < g.length; k++) out[g[k]] = Math.max(out[g[k]], out[g[k - 1]] + h);
    const last = g[g.length - 1];
    if (out[last] > bottom - h / 2) {
      out[last] = bottom - h / 2;
      for (let k = g.length - 2; k >= 0; k--) out[g[k]] = Math.min(out[g[k]], out[g[k + 1]] - h);
    }
    if (out[g[0]] < top + h / 2) {
      out[g[0]] = top + h / 2; // more labels than fit: keep the order, let the bottom ones overflow
      for (let k = 1; k < g.length; k++) out[g[k]] = Math.max(out[g[k]], out[g[k - 1]] + h);
    }
  }
  return out;
}

/** Where each drawn outlier's line ends: (step index, value), or null when it has no value. */
export function outlierEnds(d: FleetData): ({ j: number; v: number } | null)[] {
  return d.outliers.map((o) => {
    let j = o.values.length - 1;
    while (j >= 0 && o.values[j] === null) j--;
    return j < 0 ? null : { j, v: o.values[j] as number };
  });
}

/** The interquartile ribbon of behaviour group `k`. */
export const groupFill = (dark: boolean, k: number): string => rgba(groupStyle(dark, k).fill, GROUP_ALPHA[dark ? "dark" : "light"]);

/** Where each group's median line ends: (step index, value), for its end label. */
export function groupEnds(d: FleetData): ({ j: number; v: number } | null)[] {
  return (d.clusters ?? []).map((c) => {
    const m = c.band.median;
    let j = m.length - 1;
    while (j >= 0 && m[j] === null) j--;
    return j < 0 ? null : { j, v: m[j] as number };
  });
}
