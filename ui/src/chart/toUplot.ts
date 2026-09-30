import type uPlot from "uplot";
import type { SeriesData } from "../lib/api";

// Okabe-Ito: colorblind-safe categorical palette. The series budget (≤5) fits it.
export const PALETTE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9"];

export interface UplotModel {
  data: uPlot.AlignedData;
  series: uPlot.Series[];
  bands: uPlot.Band[];
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

export function toUplot(series: SeriesData[], grid?: Grid): UplotModel {
  const all = new Set<number>(grid ? gridTimes(grid) : []);
  series.forEach((s) => s.ts.forEach((t) => all.add(t)));
  const xs = [...all].sort((a, b) => a - b);
  const index = new Map(xs.map((t, i) => [t, i]));
  const data: (number | null)[][] = [xs.map((t) => t / 1000)];
  const uSeries: uPlot.Series[] = [{}];
  const bands: uPlot.Band[] = [];

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
    const avgIdx = data.length;
    data.push(column(s.avg), column(s.min), column(s.max));
    const name = seriesName(s.labels);
    uSeries.push(
      { label: name, stroke: color, width: 1.5, spanGaps: false },
      { label: `${name} min`, stroke: rgba(color, 0.35), width: 0.5, spanGaps: false, points: { show: false } },
      { label: `${name} max`, stroke: rgba(color, 0.35), width: 0.5, spanGaps: false, points: { show: false } },
    );
    bands.push({ series: [avgIdx + 2, avgIdx + 1], fill: rgba(color, 0.15) });
  });

  return { data: data as uPlot.AlignedData, series: uSeries, bands, points: xs.length * series.length };
}
