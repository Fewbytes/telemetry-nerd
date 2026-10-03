import type uPlot from "uplot";
import type { Caveat, YContext, YView } from "../lib/api";
import { sourceText, type VariationSource } from "../lib/sources";
import { resolveY, yStats, type YResolved } from "./yview";

/** Fleet panel (bead lkn.3): many series of one metric as a group band + outlying members. */
export interface FleetEpisode {
  start_ms: number; end_ms: number;
  /** strongest z of the run, signed (relative to the member's own level when beyond_own_level) */
  peak_z: number | null; sustained: boolean;
  /** a level / change outlier's excursion beyond its own (shifted, drifting) level */
  beyond_own_level: boolean;
}
/** What the end label says: offset / change / rate, as a ratio (log scale) or a difference. */
export interface FleetEffect {
  as: "ratio" | "difference"; offset: number | null; change?: number | null; at_ms?: number; change_per_hour?: number | null;
}
export interface FleetOutlier {
  id: string; labels: Record<string, string>; kind: "persistent" | "drifting" | "shifted" | "transient";
  direction: "higher" | "lower"; score: number | null; values: (number | null)[];
  since_ms: number | null; episodes: FleetEpisode[]; effect?: FleetEffect;
  /** behaviour group the member was judged in (lkn.10), when the fleet is split */
  cluster?: string;
  /** spec §5.4: special cause, or undetermined when the deviation rests on partial buckets */
  source?: VariationSource;
}
type Col = (number | null)[];
/** The SPC zones of a fleet or group: centre (per-step median) ± 2σ / 3σ, σ the tests' pooled robust sigma. */
export interface FleetZones { centre: Col; lo2: Col; hi2: Col; lo3: Col; hi3: Col }
/** The fleet's SPC reference band (nq6): the default view. */
export interface FleetSpc extends FleetZones {
  /** single-step flag threshold (spike test, fleet-wide 1%) in z and in the band's units */
  threshold_z: number | null; threshold_lo: Col | null; threshold_hi: Col | null;
  window: number; pool_half: number;
  /** "median ± 2σ/3σ (robust, pooled ±6 steps, log scale: multiplicative)" */
  legend: string;
  /** per step: member-steps beyond 3σ that no test flagged */
  outside3: number[]; outside3_note: string; tested: number;
  /** principle 4: member measurement error not propagated */
  note: string;
}
/** A behaviour group (lkn.10): its own median and interquartile band per step, and its own SPC zones. */
export interface FleetCluster {
  id: string; size: number; band: { median: Col; q25: Col; q75: Col }; spc?: FleetZones;
}
export interface FleetBand { median: Col; q25: Col; q75: Col; q10: Col; q90: Col; lo: Col; hi: Col }
/** Missing-member bounds of each quantile: `${q}_lo` / `${q}_hi`; null = unbounded where the quantile is drawn and n < alive. */
export type FleetBandBounds = Partial<Record<`${"median" | "q25" | "q75" | "q10" | "q90"}_${"lo" | "hi"}`, Col>>;
export interface FleetHeatRow { id: string; z: Col; rank: number | null; first: number; last: number }
/** Member x time matrix of z (bead lkn.11): rows already sorted; None = no report. */
export interface FleetHeat { z_cap: number; rows_total: number; rows: FleetHeatRow[] }
export interface FleetData {
  ts: number[]; members: number; normalise: "none" | "member"; scale: "log" | "linear";
  band: FleetBand; n: number[]; alive: number[]; outliers: FleetOutlier[]; outlier_count: number; heat?: FleetHeat;
  clusters?: FleetCluster[]; located?: Caveat[];
  spc?: FleetSpc; band_bounds?: FleetBandBounds;
}
/** The band view: the SPC reference (default) or the descriptive per-step quantiles. */
export type BandView = "spc" | "quantiles";

/** The SPC view needs the payload's zones; an older payload falls back to the quantiles. */
export const bandView = (d: FleetData, v: BandView): BandView => (v === "spc" && d.spc ? "spc" : "quantiles");

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
const GROUP_ALPHA = { light: 0.3, dark: 0.32 } as const;
/** A group's SPC zones: ±3σ lighter, ±2σ at the group alpha. */
const GROUP_ZONE_ALPHA = { light: [0.14, 0.3], dark: [0.16, 0.32] } as const;
export const groupStyle = (dark: boolean, k: number) => GROUP_STYLES[dark ? "dark" : "light"][k % GROUP_STYLES.light.length];

/** Time spans (ms) where the data is unknown (located `untrusted_data`): hatched, never read as values. */
export function untrustedSpans(d: FleetData): [number, number][] {
  return (d.located ?? []).filter((c) => c.code === "untrusted_data").flatMap((c) => c.where?.spans ?? []);
}

/** Okabe-Ito, colour-blind safe; the band is teal, so outliers keep the colours. */
export const OUTLIER_COLORS = ["#D55E00", "#0072B2", "#CC79A7", "#009E73", "#E69F00", "#56B4E9"];
/** A transient member's line where it is inside the band: muted grey that still clears 3:1. */
export const MUTED_LINE = { light: "#757b82", dark: "#9aa0a6" } as const;

const inEpisode = (o: FleetOutlier, ms: number): boolean => o.episodes.some((e) => ms >= e.start_ms && ms <= e.end_ms);

/** A transient member's values inside its episodes only (null elsewhere): the coloured segments. */
export const episodeValues = (d: FleetData, o: FleetOutlier): Col => o.values.map((v, i) => (inEpisode(o, d.ts[i]) ? v : null));

/** Outlier columns by mode: a level / change outlier is one coloured line ("outlier"); a transient is a
 *  muted grey line ("muted") plus its episode segments in colour ("episode"). `owner`: outlier index. */
function outlierCols(d: FleetData): { cols: Col[]; roles: string[]; owner: number[] } {
  const cols: Col[] = [], roles: string[] = [], owner: number[] = [];
  d.outliers.forEach((o, k) => {
    if (o.kind === "transient") {
      cols.push(o.values, episodeValues(d, o)); roles.push("muted", "episode"); owner.push(k, k);
    } else {
      cols.push(o.values); roles.push("outlier"); owner.push(k);
    }
  });
  return { cols, roles, owner };
}

/**
 * Columns: x (s), the band columns of the view, then the outlier columns (see outlierCols).
 * SPC (default): lo3, hi3, lo2, hi2, median (the centre), then the flag threshold lines (thrlo, thrhi)
 * when the tests ran; zones are nested fills: ±3σ outer, ±2σ inner.
 * Quantiles: min, max, q10, q90, q25, q75, median; nested fills max-min, q90-q10, q75-q25.
 * Grouped: per group its zones (SPC) or its q25, q75, median (quantiles, after the whole min-max).
 * Nulls stay nulls: a step with too few members has no band there, never interpolated.
 */
export function toFleetUplot(d: FleetData, view: BandView = "spc"): { data: uPlot.AlignedData; bands: uPlot.Band[]; roles: string[]; owner: (number | null)[] } {
  const x = d.ts.map((t) => t / 1000);
  const b = d.band;
  const v = bandView(d, view);
  let cols: Col[], roles: string[], bands: uPlot.Band[];
  if (grouped(d) && v === "spc" && d.clusters!.every((c) => c.spc)) {
    const cs = d.clusters!;
    cols = cs.flatMap((c) => [c.spc!.lo3, c.spc!.hi3, c.spc!.lo2, c.spc!.hi2, c.spc!.centre]);
    roles = cs.flatMap(() => ["lo3", "hi3", "lo2", "hi2", "cmedian"]);
    bands = cs.flatMap((_, k) => [{ series: [2 + 5 * k, 1 + 5 * k] as [number, number] }, { series: [4 + 5 * k, 3 + 5 * k] as [number, number] }]);
  } else if (grouped(d)) {
    // grouped (oyi): the whole fleet's min-max for context, then per group q25, q75, median
    const cs = d.clusters!;
    cols = [b.lo, b.hi, ...cs.flatMap((c) => [c.band.q25, c.band.q75, c.band.median])];
    roles = ["lo", "hi", ...cs.flatMap(() => ["cq25", "cq75", "cmedian"])];
    bands = [{ series: [2, 1] }, ...cs.map((_, k) => ({ series: [4 + 3 * k, 3 + 3 * k] as [number, number] }))];
  } else if (v === "spc") {
    const s = d.spc!;
    cols = [s.lo3, s.hi3, s.lo2, s.hi2, s.centre];
    roles = ["lo3", "hi3", "lo2", "hi2", "median"];
    if (s.threshold_lo && s.threshold_hi) { cols.push(s.threshold_lo, s.threshold_hi); roles.push("thrlo", "thrhi"); }
    bands = [{ series: [2, 1] }, { series: [4, 3] }];
  } else {
    cols = [b.lo, b.hi, b.q10, b.q90, b.q25, b.q75, b.median];
    roles = ["lo", "hi", "q10", "q90", "q25", "q75", "median"];
    bands = [{ series: [2, 1] }, { series: [4, 3] }, { series: [6, 5] }];
  }
  const oc = outlierCols(d);
  return {
    data: [x, ...cols, ...oc.cols] as uPlot.AlignedData, bands,
    roles: ["x", ...roles, ...oc.roles], owner: [null, ...cols.map(() => null), ...oc.owner],
  };
}

const KIND_TEXT: Record<FleetOutlier["kind"], string> = {
  persistent: "consistently", drifting: "drifting", shifted: "shifted", transient: "briefly",
};

const trim = (v: number, digits = 2): string => String(Number(v.toPrecision(digits)));

/** A ratio as a signed percent ("+38%", "−12%"), or a factor beyond ×2 / ÷2 ("×2.7"); a difference signed with its unit. */
export function effectText(v: number | null | undefined, as: FleetEffect["as"], unit: string | null = null): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "";
  if (as === "ratio") {
    if (v >= 2) return `×${trim(v)}`;
    if (v <= 0.5) return `÷${trim(1 / v)}`;
    const pct = (v - 1) * 100;
    return `${pct >= 0 ? "+" : "−"}${trim(Math.abs(pct))}%`;
  }
  return `${v >= 0 ? "+" : "−"}${trim(Math.abs(v), 3)}${unit ? ` ${unit}` : ""}`;
}

/** The strongest episode (largest |peak z|). */
const strongest = (o: FleetOutlier): FleetEpisode | null =>
  o.episodes.reduce<FleetEpisode | null>((a, e) => (!a || Math.abs(e.peak_z ?? 0) > Math.abs(a.peak_z ?? 0) ? e : a), null);

/** "spike 6.1σ 10:22–10:25 (momentary)": a momentary run is a spike, a sustained one an episode. */
export function episodeText(e: FleetEpisode, fmtTime: (ms: number) => string): string {
  const what = e.sustained ? "episode" : "spike";
  const z = e.peak_z === null ? "" : ` ${trim(Math.abs(e.peak_z))}σ`;
  const own = e.beyond_own_level ? " beyond own level" : "";
  const at = e.start_ms === e.end_ms ? fmtTime(e.start_ms) : `${fmtTime(e.start_ms)}–${fmtTime(e.end_ms)}`;
  return `${what}${z}${own} ${at} (${e.sustained ? "sustained" : "momentary"})`;
}

/** The end label's mode text: kind + effect for level / change outliers ("+38% since 09:10", "drifting +2%/h"),
 *  the strongest episode for a transient; a level / change outlier with episodes gets both. */
export function modeText(o: FleetOutlier, fmtTime: (ms: number) => string, unit: string | null = null): string {
  const ef = o.effect;
  const more = o.episodes.length > 1 ? ` +${o.episodes.length - 1} more` : "";
  const ep = strongest(o);
  if (o.kind === "transient") return ep ? `${episodeText(ep, fmtTime)}${more}` : `briefly ${o.direction}`;
  let head: string;
  if (o.kind === "drifting") {
    const rate = ef ? effectText(ef.change_per_hour, ef.as, unit) : "";
    head = rate ? `drifting ${rate}/h` : `drifting ${o.direction}`;
  } else if (o.kind === "shifted") {
    const ch = ef ? effectText(ef.change, ef.as, unit) : "";
    const at = ef?.at_ms ?? o.since_ms;
    head = `shifted${ch ? ` ${ch}` : ` ${o.direction}`}${at != null ? ` at ${fmtTime(at)}` : ""}`;
  } else {
    const off = ef ? effectText(ef.offset, ef.as, unit) : "";
    head = `${off || `consistently ${o.direction}`}${o.since_ms !== null ? ` since ${fmtTime(o.since_ms)}` : ""}`;
  }
  return ep ? `${head} · ${episodeText(ep, fmtTime)}${more}` : head;
}

export function outlierText(o: FleetOutlier, fmtTime: (ms: number) => string, unit: string | null = null): string {
  const group = o.cluster ? ` within group ${o.cluster}` : "";
  const src = o.source && o.source !== "special_cause" ? ` (${sourceText(o.source)})` : "";
  return `${o.id}: ${KIND_TEXT[o.kind]} ${o.direction}${group} · ${modeText(o, fmtTime, unit)}${src}`;
}

/** Steps with alive members not reporting: where the quantile view draws missing-member bounds. */
export const missingSteps = (d: FleetData): number[] => d.ts.flatMap((_, i) => (d.alive[i] > d.n[i] ? [i] : []));

/** The missing-member bounds of each drawn quantile at step `j`: [lo, hi], ±Infinity where unbounded. */
export function boundsAt(d: FleetData, j: number): { q: string; lo: number; hi: number }[] {
  const out: { q: string; lo: number; hi: number }[] = [];
  if (!(d.alive[j] > d.n[j])) return out;
  for (const q of ["q10", "q25", "median", "q75", "q90"] as const) {
    const v = d.band[q][j];
    if (v === null || v === undefined) continue;
    const lo = d.band_bounds?.[`${q}_lo`]?.[j] ?? null, hi = d.band_bounds?.[`${q}_hi`]?.[j] ?? null;
    out.push({ q, lo: lo ?? -Infinity, hi: hi ?? Infinity });
  }
  return out;
}

const Q_LABEL: Record<string, string> = { q10: "10%", q25: "25%", median: "median", q75: "75%", q90: "90%" };
const num = (v: number): string => String(Number(v.toPrecision(4)));

/** Quantile-view hover text at a step with missing members: each quantile's bounds, min / max unknown beyond. */
export function boundsText(d: FleetData, j: number, fmtTime: (ms: number) => string): string | null {
  if (!(d.alive[j] > d.n[j])) return null;
  const parts = boundsAt(d, j).map(({ q, lo, hi }) => {
    const l = Number.isFinite(lo) ? num(lo) : "unbounded", h = Number.isFinite(hi) ? num(hi) : "unbounded";
    return `${Q_LABEL[q]} ${l}–${h}`;
  });
  return [`${fmtTime(d.ts[j])} · ${d.n[j]} of ${d.alive[j]} alive reporting`, ...parts, "min / max: unknown beyond (missing members)"].join(" · ");
}

/** Is outlier `o`'s value at step `j` beyond the drawn 3σ zone without being flagged there (a transient's
 *  step outside its episodes)? Level / change outliers are flagged as a whole. */
export function outsideUnflagged(d: FleetData, o: FleetOutlier, j: number): boolean {
  const s = d.spc, v = o.values[j];
  if (!s || v === null || o.kind !== "transient" || inEpisode(o, d.ts[j])) return false;
  const lo = s.lo3[j], hi = s.hi3[j];
  return (lo !== null && v < lo) || (hi !== null && v > hi);
}

/** Rug cells of the SPC view: steps where members lie beyond 3σ unflagged (`count` members). */
export const outsideRug = (d: FleetData): { x: number; j: number; count: number }[] =>
  (d.spc?.outside3 ?? []).flatMap((c, j) => (c > 0 ? [{ x: d.ts[j] / 1000, j, count: c }] : []));

export function fleetLegend(d: FleetData, view: BandView = "spc"): string {
  const v = bandView(d, view);
  const shown = d.outliers.length;
  const more = d.outlier_count - shown;
  const nMin = Math.min(...d.n), nMax = Math.max(...d.n);
  const n = nMin === nMax ? `${nMax} reporting per step` : `${nMin}–${nMax} reporting per step`;
  const units = d.normalise === "member" ? " · each member relative to its own median" : "";
  const out = d.outlier_count === 0 ? "no outliers" : `${d.outlier_count} outlier${d.outlier_count > 1 ? "s" : ""}${more > 0 ? ` (${shown} drawn)` : ""}`;
  const marks = d.outlier_count ? " · coloured line: consistently off, shifted or drifting; grey line with coloured, bracketed segments: transient episodes" : "";
  // principle 4, the short form: the full sentence is in the summary
  const note = d.spc ? ` · ${d.spc.note.split(" (")[0]}` : "";
  if (grouped(d)) {
    const gs = d.clusters!.map((c) => `${c.id} (${c.size})`).join(", ");
    const shade = v === "spc"
      ? "per group its median ± 2σ/3σ (its own tests' pooled robust σ; the common-cause envelope); flag thresholds per group in the summary"
      : "min–max across all members; per group 25–75% and its median (common-cause envelope, descriptive)";
    return `${d.members} members in ${d.clusters!.length} behaviour groups (systemic structure): ${gs} · ${out}, each judged within its group (special causes) · ${n} · shading: ${shade}${marks}${units}${note}`;
  }
  if (v === "spc") {
    const s = d.spc!;
    const thr = s.threshold_z !== null ? ` · dashed: flag threshold ${trim(s.threshold_z)}σ (single step, fleet-wide 1%; level, change and episode tests flag inside it too)` : " · no outlier tests ran";
    const rug = s.outside3.some((c) => c > 0) ? ` · top ticks: ${s.outside3_note}` : "";
    return `${d.members} members · ${out}${d.outlier_count ? " (special causes)" : ""} · ${n} · zones: ${s.legend}, the common-cause envelope (a stable reading aid, not the flag rule)${thr}${rug}${marks}${units}${note}`;
  }
  const bounds = missingSteps(d).length ? " · lighter: missing-member bounds (members alive but not reporting at ±∞; unbounded past their rank)" : "";
  return `${d.members} members · ${out}${d.outlier_count ? " (special causes)" : ""} · ${n} · shading: min–max, 10–90%, 25–75% across reporting members (descriptive: these members only) · line: median${bounds}${marks}${units}${note}`;
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
/** Opacity of the ±3σ and ±2σ zones: outer lighter. */
export const ZONE_ALPHAS = { light: [0.14, 0.32], dark: [0.18, 0.38] } as const;
/** Opacity of one missing-member bound cell (they stack: overlapping quantile bounds read darker). */
export const BOUND_ALPHA = { light: 0.09, dark: 0.12 } as const;

const rgba = (hex: string, a: number): string => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
};

/** Nested ribbon fills in the single fleet hue, outermost first: quantiles (min-max, 10-90, 25-75) or zones (±3σ, ±2σ). */
export const bandFills = (dark: boolean, view: BandView = "quantiles"): string[] => {
  const m = dark ? "dark" : "light";
  return (view === "spc" ? ZONE_ALPHAS : BAND_ALPHAS)[m].map((a) => rgba(FLEET_HUE[m].fill, a));
};

/** The missing-member bound fill (lighter than the quantile ribbons). */
export const boundFill = (dark: boolean): string => rgba(FLEET_HUE[dark ? "dark" : "light"].fill, BOUND_ALPHA[dark ? "dark" : "light"]);

/** `line`: the line colour and CSS border style drawn across the swatch. */
export interface KeyEntry { id: string; label: string; swatch: string; line?: { color: string; style: "solid" | "dashed" | "dotted" }; hatch?: boolean }
const cssDash = (dash: readonly number[]): "solid" | "dashed" | "dotted" => (!dash.length ? "solid" : dash[0] <= 2 ? "dotted" : "dashed");
/** Dash of the flag threshold line (canvas px, times dpr). */
export const THRESHOLD_DASH = [5, 4];
export function fleetKey(dark: boolean, d?: FleetData, view: BandView = "spc"): KeyEntry[] {
  const m = dark ? "dark" : "light";
  const v = d ? bandView(d, view) : "quantiles";
  const unknown: KeyEntry[] = d && untrustedSpans(d).length
    ? [{ id: "unknown", label: "hatched: data unknown", swatch: dark ? "#9aa0a6" : "#6b7075", hatch: true }]
    : [];
  const marks: KeyEntry[] = !d || d.outliers.some((o) => o.kind !== "transient") || !d.outliers.length
    ? [{ id: "outlier", label: "outlier member: consistently off / shifted / drifting (hover for id)", swatch: OUTLIER_COLORS[0] }]
    : [];
  if (d?.outliers.some((o) => o.kind === "transient" || o.episodes.length)) {
    marks.push({ id: "transient", label: "transient: grey line, episodes coloured and bracketed", swatch: MUTED_LINE[m], line: { color: OUTLIER_COLORS[0], style: "solid" } });
  }
  if (d && grouped(d)) {
    const spc = v === "spc" && d.clusters!.every((c) => c.spc);
    return [
      ...(spc ? [] : [{ id: "minmax", label: "min–max (all)", swatch: bandFills(dark)[0] }]),
      ...d.clusters!.map((c, k) => {
        const g = groupStyle(dark, k);
        return {
          id: `group-${c.id}`, label: `group ${c.id} (${c.size}): ${spc ? "median ± 2σ/3σ" : "25–75% + median"}`, swatch: rgba(g.fill, GROUP_ALPHA[m]),
          line: { color: g.line, style: cssDash(g.dash) },
        };
      }),
      ...marks,
      ...unknown,
    ];
  }
  if (d && v === "spc") {
    const f = bandFills(dark, "spc");
    const s = d.spc!;
    return [
      { id: "z3", label: "±3σ", swatch: f[0] },
      { id: "z2", label: "±2σ", swatch: f[1] },
      { id: "median", label: "median (centre)", swatch: FLEET_HUE[m].line },
      ...(s.threshold_z !== null ? [{ id: "flag", label: `flag threshold ${trim(s.threshold_z)}σ (fleet-wide 1%)`, swatch: "transparent", line: { color: MUTED_LINE[m], style: "dashed" as const } }] : []),
      ...(s.outside3.some((c) => c > 0) ? [{ id: "rug", label: "tick: beyond 3σ, not significant", swatch: MUTED_LINE[m] }] : []),
      ...marks,
      ...unknown,
    ];
  }
  const f = bandFills(dark);
  return [
    { id: "minmax", label: "min–max", swatch: f[0] },
    { id: "q1090", label: "10–90%", swatch: f[1] },
    { id: "q2575", label: "25–75%", swatch: f[2] },
    { id: "median", label: "median", swatch: FLEET_HUE[m].line },
    ...(d && missingSteps(d).length ? [{ id: "bounds", label: "lighter: missing-member bounds", swatch: boundFill(dark) }] : []),
    ...marks,
    ...unknown,
  ];
}

/** Names the chart type and its encoding, so it is legible without reading the long legend. */
export function fleetAxisLabel(d: FleetData, unit: string | null, view: BandView = "spc"): string {
  const u = d.normalise === "member" ? "× own median" : unit || "value";
  const v = bandView(d, view);
  if (grouped(d)) {
    return v === "spc" && d.clusters!.every((c) => c.spc)
      ? `${u} · ${d.members} members in ${d.clusters!.length} behaviour groups (per group: median ± 2σ/3σ, its own pooled robust σ)`
      : `${u} · ${d.members} members in ${d.clusters!.length} behaviour groups (per group: 25–75 band + median; all: min–max)`;
  }
  if (v === "spc") return `${u} · ${d.spc!.legend} across ${d.members} members`;
  return `${u} · spread across ${d.members} members (bands: 25–75, 10–90, min–max)`;
}

/** Steps where an outlier sample is isolated (null on both sides): a line cannot show it, so it gets a dot.
 *  Every other sample is part of the drawn line; markers appear only on hover. */
export function outlierMarkIdx(d: FleetData, k: number): number[] {
  const o = d.outliers[k];
  return o ? isolatedIdx(o.values) : [];
}

/** Indices of values with null on both sides (a line cannot draw them). */
export const isolatedIdx = (v: Col): number[] =>
  v.flatMap((x, i) => (x !== null && (i === 0 || v[i - 1] === null) && (i === v.length - 1 || v[i + 1] === null) ? [i] : []));

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
  const mk = (id: string, avg: (number | null)[]) => ({ id, labels: {}, ts: d.ts, avg, min: avg, max: avg, count: avg.map(() => 1) });
  const series = [mk("lo", d.band.lo), mk("hi", d.band.hi), ...d.outliers.map((o) => mk(o.id, o.values))];
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

/** Where a line ends: (step index, value) of its last non-null value, or null when it has none. */
function lineEnd(values: (number | null)[]): { j: number; v: number } | null {
  let j = values.length - 1;
  while (j >= 0 && values[j] === null) j--;
  return j < 0 ? null : { j, v: values[j] as number };
}

/** Where each drawn outlier's line ends, for its end label. */
export const outlierEnds = (d: FleetData): ({ j: number; v: number } | null)[] => d.outliers.map((o) => lineEnd(o.values));

/** The interquartile ribbon of behaviour group `k`. */
export const groupFill = (dark: boolean, k: number): string => rgba(groupStyle(dark, k).fill, GROUP_ALPHA[dark ? "dark" : "light"]);

/** The ±3σ and ±2σ zones of behaviour group `k` (outer lighter). */
export const groupZoneFills = (dark: boolean, k: number): string[] =>
  GROUP_ZONE_ALPHA[dark ? "dark" : "light"].map((a) => rgba(groupStyle(dark, k).fill, a));

/** Where each group's median (centre) line ends, for its end label. */
export const groupEnds = (d: FleetData, view: BandView = "spc"): ({ j: number; v: number } | null)[] =>
  (d.clusters ?? []).map((c) => lineEnd(bandView(d, view) === "spc" && c.spc ? c.spc.centre : c.band.median));
