import type uPlot from "uplot";
import { PALETTE, seriesName } from "./toUplot";
import { fmtPeriod } from "./period";

export interface SpectrumPeak { period_s: number; interval_s: [number, number]; power: number; significant: boolean; fap: number }
export interface SpectrumSeries {
  id: string; labels: Record<string, string>; periods_s: number[]; power: number[]; level: number;
  peaks: SpectrumPeak[]; caveats: string[];
}
export interface SpectrumData { series: SpectrumSeries[]; limits: { shortest_s: number; longest_s: number } }

/** Power against period (log x), one line per series, dashed 1% false-alarm level. Series may have different grids. */
export function toSpectrumUplot(d: SpectrumData): { data: uPlot.AlignedData; series: uPlot.Series[]; xRange: [number, number] } {
  const xs = [...new Set(d.series.flatMap((s) => s.periods_s))].sort((a, b) => a - b);
  const index = new Map(xs.map((x, i) => [x, i]));
  const data: (number | null)[][] = [xs];
  const series: uPlot.Series[] = [{}];
  d.series.forEach((s, k) => {
    const col: (number | null)[] = Array(xs.length).fill(null);
    s.periods_s.forEach((p, i) => (col[index.get(p)!] = s.power[i]));
    data.push(col);
    series.push({ label: seriesName(s.labels), stroke: PALETTE[k % PALETTE.length], width: 1.5, spanGaps: true });
  });
  const lvl = Math.max(...d.series.map((s) => s.level));
  data.push(xs.map(() => lvl));
  series.push({ label: "1% false-alarm level (white noise)", stroke: "#888", width: 1, dash: [4, 4], points: { show: false } });
  return { data: data as uPlot.AlignedData, series, xRange: [d.limits.shortest_s / 1.6, d.limits.longest_s * 1.6] };
}

export function limitZones(limits: { shortest_s: number; longest_s: number }, xmin: number, xmax: number) {
  return [
    { from: xmin, to: limits.shortest_s, text: "< 2×step: invisible at this step" },
    { from: limits.longest_s, to: xmax, text: "> range/2: needs a longer range" },
  ].filter((z) => z.to > z.from);
}

export function peakMarks(s: SpectrumSeries) {
  return s.peaks.filter((p) => p.significant).map((p) => ({
    x: p.period_s, lo: p.interval_s[0], hi: p.interval_s[1], y: p.power,
    text: `${fmtPeriod(p.period_s)} [${fmtPeriod(p.interval_s[0])}–${fmtPeriod(p.interval_s[1])}]`,
  }));
}
