import type uPlot from "uplot";
import type { SeriesData } from "../lib/api";

// Okabe-Ito: colorblind-safe categorical palette. The series budget (≤5) fits it.
export const PALETTE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9"];

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

function rgba(hex: string, alpha: number): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

const fmt = (v: number): string => String(Number(v.toPrecision(4)));

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

export interface ToUplotOpts {
  quantile?: boolean;
  nMin?: number | null;
  /** another series set drawn with these: the raw behind a filter (faint, underneath) or the removed part (dashed, on top) */
  context?: { role: "raw" | "removed"; series: SeriesData[] };
  /** filter edge spans [t0, t1] ms (all series, or per series id): drawn dashed, they are unreliable */
  edges?: number[][] | Record<string, number[][]>;
}

export function toUplot(series: SeriesData[], grid?: Grid, opts: ToUplotOpts = {}): UplotModel {
  const all = new Set<number>(grid ? gridTimes(grid) : []);
  series.forEach((s) => s.ts.forEach((t) => all.add(t)));
  opts.context?.series.forEach((s) => s.ts.forEach((t) => all.add(t)));
  const xs = [...all].sort((a, b) => a - b);
  const index = new Map(xs.map((t, i) => [t, i]));
  const data: (number | null)[][] = [xs.map((t) => t / 1000)];
  const uSeries: uPlot.Series[] = [{}];
  const bands: uPlot.Band[] = [];
  const legendHidden: number[] = [];

  const contextColumn = (c: SeriesData, f: "avg" | "min" | "max") => {
    const out: (number | null)[] = Array(xs.length).fill(null);
    c.ts.forEach((t, i) => {
      const idx = index.get(t);
      if (idx !== undefined) out[idx] = c[f][i];
    });
    return out;
  };

  series.forEach((s, k) => {
    const color = PALETTE[k % PALETTE.length];
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
      uSeries.push({ label: name, stroke: color, width: 1.5, spanGaps: false });
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
        { label: `${name} raw`, stroke: rgba(color, 0.35), width: 1, spanGaps: false },
        { label: "", stroke: "transparent", width: 0, points: { show: false } },
        { label: "", stroke: "transparent", width: 0, points: { show: false } },
      );
      bands.push({ series: [ci + 2, ci + 1], fill: rgba(color, 0.08) });
    }
    const spans = Array.isArray(opts.edges) ? opts.edges : (opts.edges?.[s.id] ?? []);
    const inEdge = (t: number) => spans.some(([a, b]) => t >= a && t <= b);
    const edgeAvg = spans.length ? s.ts.map((t, i) => (inEdge(t) ? s.avg[i] : null)) : null;
    const mainAvg = edgeAvg ? s.avg.map((v, i) => (inEdge(s.ts[i]) ? null : v)) : s.avg;
    const avgIdx = data.length;
    data.push(column(mainAvg), column(s.min), column(s.max));
    // one legend row per series: the value plus its envelope, instead of separate min/max rows
    const withEnvelope = (_u: uPlot, v: number | null, _si: number, i: number | null) => {
      if (v == null || i == null) return "--";
      const lo = data[avgIdx + 1][i], hi = data[avgIdx + 2][i];
      return lo == null || hi == null ? fmt(v) : `${fmt(v)} [${fmt(lo)}–${fmt(hi)}]`;
    };
    legendHidden.push(avgIdx + 1, avgIdx + 2);
    uSeries.push(
      { label: name, stroke: color, width: 1.5, spanGaps: false, value: withEnvelope },
      { label: `${name} min`, stroke: rgba(color, 0.35), width: 0.5, spanGaps: false, points: { show: false } },
      { label: `${name} max`, stroke: rgba(color, 0.35), width: 0.5, spanGaps: false, points: { show: false } },
    );
    bands.push({ series: [avgIdx + 2, avgIdx + 1], fill: rgba(color, 0.15) });
    if (edgeAvg) {
      // filter edges (series start/end, around gaps): truncated kernel, unreliable: dashed twin
      data.push(column(edgeAvg));
      legendHidden.push(uSeries.length);
      uSeries.push({ label: `${name} (filter edge, unreliable)`, stroke: color, width: 1.5, dash: [4, 4], spanGaps: false });
    }
    if (ctx && opts.context?.role === "removed") {
      // the part a high/band-pass took out, dashed over the raw series
      data.push(contextColumn(ctx, "avg"));
      uSeries.push({ label: `${name} removed part`, stroke: rgba(color, 0.8), width: 1, dash: [3, 3], spanGaps: false });
    }
  });

  return { data: data as uPlot.AlignedData, series: uSeries, bands, legendHidden, points: xs.length * series.length };
}
