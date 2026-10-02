import type uPlot from "uplot";
import type { SeriesData } from "../lib/api";
import type { OverlayDraw } from "./overlays";

// Okabe-Ito: colorblind-safe categorical palette. The series budget (≤5) fits it.
// Dark-theme values: all ≥3:1 against --bg #16181d (WCAG SC 1.4.11).
export const PALETTE_DARK = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9"];
// Light-theme values: same hues, darkened just enough that every entry still clears
// 3:1 against --bg #ffffff (the full-saturation orange and sky-blue fail there — see
// docs/telemetry-graphing-guide.md §6). Hue order is unchanged; verified by
// colormap.test.ts's contrast assertions.
export const PALETTE_LIGHT = ["#0072B2", "#BB8100", "#009E73", "#C870A1", "#1C95D9"];
// Back-compat default for callers without a theme (dark-safe values). Prefer
// `seriesPalette(mode)` wherever the current theme is known.
export const PALETTE = PALETTE_DARK;

export function seriesPalette(mode: "light" | "dark"): string[] {
  return mode === "light" ? PALETTE_LIGHT : PALETTE_DARK;
}

export interface UplotModel {
  data: uPlot.AlignedData;
  series: uPlot.Series[];
  bands: uPlot.Band[];
  /** series indices whose legend row is hidden (envelope edges, faded low-n twins) */
  legendHidden: number[];
  points: number;
}

export function seriesName(labels: Record<string, string>): string {
  const { __name__, ...rest } = labels;
  const inner = Object.entries(rest)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}="${v}"`)
    .join(",");
  return `${__name__ ?? ""}{${inner}}`;
}

export function rgba(hex: string, alpha: number): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

const fmt = (v: number): string => String(Number(v.toPrecision(4)));

// With spanGaps:false, a sample flanked by nulls on both sides has no line segment to
// draw (stepped or not), so uPlot's density-based auto marker can skip it too and the
// sample vanishes. Force a marker for exactly those indices; defer to the default
// show-all-or-none behaviour otherwise (telemetry-graphing-guide.md §7).
export const isolatedPointsFilter = (u: uPlot, seriesIdx: number, show: boolean): number[] | null => {
  if (show) return null;
  const ydata = u.data[seriesIdx] as (number | null)[];
  const idxs: number[] = [];
  for (let i = 0; i < ydata.length; i++) {
    if (ydata[i] != null && ydata[i - 1] == null && ydata[i + 1] == null) idxs.push(i);
  }
  return idxs.length ? idxs : null;
};

export interface Grid {
  start: number; // ms
  end: number; // ms, inclusive
  step: number; // effective step ms
}

// Full bucket grid, so holes in the data become nulls (uPlot would otherwise
// draw a line straight across a missing bucket).
function gridTimes({ start, end, step }: Grid): number[] {
  if (!(step > 0) || !(end >= start)) return [];
  const out: number[] = [];
  for (let t = Math.ceil(start / step) * step; t <= end; t += step) out.push(t);
  return out;
}

const EPS_S = 0.001;

/** Bucket ends -> interval edges: value i spans (x_i - step, x_i]; lone buckets stay visible. */
export function stepify(data: (number | null)[][], stepS: number): (number | null)[][] {
  const [xs, ...cols] = data;
  const x2: number[] = [];
  let prev = -Infinity;
  for (const x of xs as number[]) {
    // an off-grid x within one step of its predecessor must not start before it ends
    x2.push(Math.max(x - stepS + EPS_S, prev + EPS_S), x);
    prev = x;
  }
  return [x2, ...cols.map((c) => c.flatMap((v) => [v, v]))];
}

export interface ToUplotOpts {
  /** draw each bucket across its interval (default: true when a grid is given) */
  stepped?: boolean;
  quantile?: boolean;
  nMin?: number | null;
  /** another series set drawn with these: the raw behind a filter (faint, underneath) or the removed part (dashed, on top) */
  context?: { role: "raw" | "removed"; series: SeriesData[] };
  /** filter edge spans [t0, t1] ms (all series, or per series id): drawn dashed, they are unreliable */
  edges?: number[][] | Record<string, number[][]>;
  /** reference layers (2as.11): normal band under each series, last-week ghost, limit line */
  overlays?: OverlayDraw;
  /** per-theme series colours (seriesPalette(mode)); defaults to the dark-safe PALETTE */
  palette?: string[];
}

export const LIMIT_COLOR = "#D55E00"; // Okabe-Ito vermilion: a hazard, not a series colour
// thresholds by tone: distinct from series colours and from the limit; ≥3:1 on both themes
export const THRESHOLD_COLORS = { bad: "#C2185B", warn: "#B26B00", info: "#0072B2" } as const;
export const REFERENCE_COLOR = "#7A7F87";

export function lineStyle(l: { kind?: string; tone?: string | null }): { stroke: string; width: number; dash: number[] } {
  if (l.kind === "threshold") return { stroke: THRESHOLD_COLORS[(l.tone as keyof typeof THRESHOLD_COLORS) ?? "info"] ?? THRESHOLD_COLORS.info, width: 1.5, dash: [3, 3] };
  if (l.kind === "reference") return { stroke: REFERENCE_COLOR, width: 1, dash: [2, 4] };
  return { stroke: LIMIT_COLOR, width: 1.5, dash: [8, 4] };
}

export function toUplot(series: SeriesData[], grid?: Grid, opts: ToUplotOpts = {}): UplotModel {
  const all = new Set<number>(grid ? gridTimes(grid) : []);
  series.forEach((s) => s.ts.forEach((t) => all.add(t)));
  opts.context?.series.forEach((s) => s.ts.forEach((t) => all.add(t)));
  const ov = opts.overlays;
  Object.values(ov?.normal ?? {}).forEach((b) => b.ts.forEach((t) => all.add(t)));
  ov?.lines?.forEach((l) => l.series?.forEach((s) => s.ts.forEach((t) => all.add(t))));
  ov?.ghost?.forEach((s) => s.ts.forEach((t) => all.add(t)));
  const xs = [...all].sort((a, b) => a - b);
  const index = new Map(xs.map((t, i) => [t, i]));
  const data: (number | null)[][] = [xs.map((t) => t / 1000)];
  // what uPlot gets (stepped or not); legend callbacks index into this, not into `data`
  let final: (number | null)[][] = data;
  const uSeries: uPlot.Series[] = [{}];
  const bands: uPlot.Band[] = [];
  const legendHidden: number[] = [];
  const palette = opts.palette ?? PALETTE;

  const contextColumn = (c: SeriesData, f: "avg" | "min" | "max") => {
    const out: (number | null)[] = Array(xs.length).fill(null);
    c.ts.forEach((t, i) => {
      const idx = index.get(t);
      if (idx !== undefined) out[idx] = c[f][i];
    });
    return out;
  };

  const onGrid = (ts: number[], values: (number | null)[]) => {
    const out: (number | null)[] = Array(xs.length).fill(null);
    ts.forEach((t, i) => {
      const idx = index.get(t);
      if (idx !== undefined) out[idx] = values[i];
    });
    return out;
  };

  series.forEach((s, k) => {
    const color = palette[k % palette.length];
    const base = s.labels ? seriesName(s.labels) : s.id;
    const band = ov?.normal?.[s.id];
    if (band) {
      // the normal range for this hour: faint, underneath everything; the chips are its legend
      const bi = data.length;
      data.push(onGrid(band.ts, band.lo), onGrid(band.ts, band.hi));
      legendHidden.push(bi, bi + 1);
      uSeries.push(
        { label: `${base} normal low`, stroke: "transparent", width: 0, points: { show: false }, spanGaps: false },
        { label: `${base} normal high`, stroke: "transparent", width: 0, points: { show: false }, spanGaps: false },
      );
      bands.push({ series: [bi + 1, bi], fill: rgba(color, 0.08) });
    }
    const ghost = ov?.ghost?.find((g) => g.id === s.id);
    if (ghost) {
      // the same window a week ago, pointwise; percentile buckets with too few observations stay out
      const nMin = opts.quantile ? (opts.nMin ?? null) : null;
      const vals = ghost.avg.map((v, i) => (nMin !== null && (ghost.count[i] ?? 0) < nMin ? null : v));
      legendHidden.push(data.length);
      data.push(onGrid(ghost.ts, vals));
      uSeries.push({
        label: `${base} last week`,
        stroke: rgba(color, 0.45),
        width: 1,
        dash: [2, 4],
        spanGaps: false,
        points: { filter: isolatedPointsFilter },
      });
    }
    const column = (values: (number | null)[]) => {
      const out: (number | null)[] = Array(xs.length).fill(null);
      // xs contains every s.ts, so every lookup hits
      s.ts.forEach((t, i) => {
        const idx = index.get(t);
        if (idx !== undefined) out[idx] = values[i];
      });
      return out;
    };
    const name = seriesName(s.labels);
    if (opts.quantile) {
      // Percentiles are never aggregated: no min/max envelope. Buckets with too few
      // observations are drawn faded so they are not read as real percentiles.
      const nMin = opts.nMin ?? null;
      const ok = s.avg.map((v, i) => (nMin === null || (s.count[i] ?? 0) >= nMin ? v : null));
      data.push(column(ok));
      uSeries.push({ label: name, stroke: color, width: 1.5, spanGaps: false, points: { filter: isolatedPointsFilter } });
      if (nMin !== null) {
        const low = s.avg.map((v, i) => ((s.count[i] ?? 0) < nMin ? v : null));
        data.push(column(low));
        legendHidden.push(uSeries.length);
        uSeries.push({
          label: `${name} (n<${nMin}, not meaningful)`,
          stroke: rgba(color, 0.35),
          width: 1,
          dash: [4, 4],
          spanGaps: false,
          points: { filter: isolatedPointsFilter },
        });
      }
      return;
    }
    const ctx = opts.context?.series.find((c) => c.id === s.id);
    if (ctx && opts.context?.role === "raw") {
      // the raw series behind a filter: faint and underneath (drawn first)
      const ci = data.length;
      data.push(contextColumn(ctx, "avg"), contextColumn(ctx, "min"), contextColumn(ctx, "max"));
      legendHidden.push(ci + 1, ci + 2);
      uSeries.push(
        { label: `${name} raw`, stroke: rgba(color, 0.35), width: 1, spanGaps: false, points: { filter: isolatedPointsFilter } },
        { label: "", stroke: "transparent", width: 0, points: { show: false } },
        { label: "", stroke: "transparent", width: 0, points: { show: false } },
      );
      bands.push({ series: [ci + 2, ci + 1], fill: rgba(color, 0.08) });
    }
    const spans = Array.isArray(opts.edges) ? opts.edges : (opts.edges?.[s.id] ?? []);
    const inEdge = (t: number) => spans.some(([a, b]) => t >= a && t <= b);
    const edgeAvg = spans.length ? s.ts.map((t, i) => (inEdge(t) ? s.avg[i] : null)) : null;
    const mainAvg = edgeAvg ? s.avg.map((v, i) => (inEdge(s.ts[i]) ? null : v)) : s.avg;
    // a declared interval (code outputs) is the band; otherwise the min-max envelope is
    const interval = s.lo && s.hi ? { lo: s.lo, hi: s.hi } : null;
    const avgIdx = data.length;
    data.push(column(mainAvg), column(interval ? interval.lo : s.min), column(interval ? interval.hi : s.max));
    // one legend row per series: the value plus its envelope, instead of separate min/max rows
    const withEnvelope = (_u: uPlot, v: number | null, _si: number, i: number | null) => {
      if (v == null || i == null) return "--";
      const lo = final[avgIdx + 1][i], hi = final[avgIdx + 2][i];
      return lo == null || hi == null ? fmt(v) : `${fmt(v)} [${fmt(lo)}–${fmt(hi)}]`;
    };
    legendHidden.push(avgIdx + 1, avgIdx + 2);
    uSeries.push(
      { label: name, stroke: color, width: 1.5, spanGaps: false, value: withEnvelope, points: { filter: isolatedPointsFilter } },
      { label: `${name} ${interval ? "lo" : "min"}`, stroke: rgba(color, 0.35), width: 0.5, spanGaps: false, points: { show: false } },
      { label: `${name} ${interval ? "hi" : "max"}`, stroke: rgba(color, 0.35), width: 0.5, spanGaps: false, points: { show: false } },
    );
    bands.push({ series: [avgIdx + 2, avgIdx + 1], fill: rgba(color, 0.15) });
    if (interval && (s.min.some((v) => v != null) || s.max.some((v) => v != null))) {
      // the code also gave the bucket's min/max: faint dotted edges, no fill (the band is the interval)
      legendHidden.push(data.length, data.length + 1);
      data.push(column(s.min), column(s.max));
      uSeries.push(
        { label: `${name} min`, stroke: rgba(color, 0.35), width: 0.5, dash: [1, 3], spanGaps: false, points: { show: false } },
        { label: `${name} max`, stroke: rgba(color, 0.35), width: 0.5, dash: [1, 3], spanGaps: false, points: { show: false } },
      );
    }
    if (edgeAvg) {
      // filter edges (series start/end, around gaps): truncated kernel, unreliable: dashed twin
      data.push(column(edgeAvg));
      legendHidden.push(uSeries.length);
      uSeries.push({
        label: `${name} (filter edge, unreliable)`,
        stroke: color,
        width: 1.5,
        dash: [4, 4],
        spanGaps: false,
        points: { filter: isolatedPointsFilter },
      });
    }
    if (ctx && opts.context?.role === "removed") {
      // the part a high/band-pass took out, dashed over the raw series
      data.push(contextColumn(ctx, "avg"));
      uSeries.push({
        label: `${name} removed part`,
        stroke: rgba(color, 0.8),
        width: 1,
        dash: [3, 3],
        spanGaps: false,
        points: { filter: isolatedPointsFilter },
      });
    }
  });

  ov?.lines?.forEach((l) => {
    // context lines (2as.15): a hard limit in the hazard colour, thresholds by tone, references faint
    const style = lineStyle(l);
    const name = l.label ?? l.metric;
    const columns: (number | null)[][] = l.series?.length ? l.series.map((s) => onGrid(s.ts, s.avg)) : l.value != null ? [xs.map(() => l.value!)] : [];
    columns.forEach((col, i) => {
      legendHidden.push(data.length);
      data.push(col);
      const suffix = l.series && l.series.length > 1 && l.series[i]?.labels ? ` ${seriesName(l.series[i].labels)}` : "";
      uSeries.push({ label: `${name}${suffix}`, stroke: style.stroke, width: style.width, dash: style.dash, spanGaps: false, points: { show: false } });
    });
  });

  const stepped = grid !== undefined && opts.stepped !== false;
  const out = stepped ? stepify(data, grid.step / 1000) : data;
  final = out;
  return { data: out as uPlot.AlignedData, series: uSeries, bands, legendHidden, points: xs.length * series.length };
}
