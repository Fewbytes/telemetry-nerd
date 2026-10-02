import type uPlot from "uplot";
import { PALETTE, seriesName } from "./toUplot";
import { fmtPeriod } from "./period";

export interface SpectrumPeak { period_s: number; interval_s: [number, number]; power: number; significant: boolean; fap: number; fap_red_noise?: number }
export interface SpectrumSeries {
  id: string; labels: Record<string, string>; periods_s: number[]; power: number[]; level: number;
  red_level?: number[]; ar1_phi?: number; peaks: SpectrumPeak[]; caveats: string[];
}
export interface SpectrumData { series: SpectrumSeries[]; limits: { shortest_s: number; longest_s: number } }

/** Power against period (log x), one line per series, dashed 1% false-alarm level (white noise) and a dotted
 * 1% level per series against its AR(1) red-noise background (what `significant` uses). Series may have different grids. */
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
  d.series.forEach((s, k) => {
    if (!s.red_level) return;
    const col: (number | null)[] = Array(xs.length).fill(null);
    s.periods_s.forEach((p, i) => (col[index.get(p)!] = s.red_level![i]));
    data.push(col);
    series.push({ label: `1% level, AR(1) red noise${d.series.length > 1 ? ` (${seriesName(s.labels)})` : ""}`, stroke: PALETTE[k % PALETTE.length], width: 1, dash: [1, 3], spanGaps: true, points: { show: false } });
  });
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
    x: p.period_s, lo: p.interval_s[0], hi: p.interval_s[1], y: p.power, power: p.power,
    text: `${fmtPeriod(p.period_s)} [${fmtPeriod(p.interval_s[0])}–${fmtPeriod(p.interval_s[1])}]`,
    shortText: fmtPeriod(p.period_s),
  }));
}

export interface PeakLabelInput { x: number; text: string; shortText: string; power: number }

/** Rough label width for the chart's small label font (~10px) when no real canvas measurement is given. */
export function estimateLabelWidth(text: string, charPx = 6): number {
  return text.length * charPx;
}

const MAX_LABEL_ROWS = 3;

/**
 * Arrange peak labels left-to-right in pixel space, avoiding overlapping bounding boxes.
 * The highest-power ("dominant") peak always keeps its full label. For every other peak, a full
 * label that would run into the next peak's label is first shortened (dropping the
 * confidence-interval range text); if it still collides with the previous label on its row, it
 * is stacked onto the next row down instead.
 *
 * Generic over `T` so callers can round-trip extra fields (e.g. a series index) through the
 * returned, re-sorted list; `x` values need not be unique.
 */
export function layoutPeakLabels<T extends PeakLabelInput>(
  marks: T[],
  toPx: (x: number) => number,
  widthOf: (text: string) => number = estimateLabelWidth,
  gap = 4,
): (T & { row: number })[] {
  if (!marks.length) return [];
  const dominant = marks.reduce((a, b) => (b.power > a.power ? b : a));
  const sorted = marks
    .map((m) => ({ m: { ...m }, xpx: toPx(m.x), isDominant: m === dominant }))
    .sort((a, b) => a.xpx - b.xpx);

  // Pass 1: shorten a non-dominant label whose full box would run into the next peak's x.
  for (let i = 0; i < sorted.length - 1; i++) {
    const cur = sorted[i], next = sorted[i + 1];
    if (!cur.isDominant && cur.xpx + widthOf(cur.m.text) + gap > next.xpx) cur.m.text = cur.m.shortText;
  }

  // Pass 2: stack anything that still collides with the last label placed on its row.
  const rowEnd: number[] = [];
  const out: (T & { row: number })[] = [];
  for (const { m, xpx } of sorted) {
    let row = 0;
    while (row < MAX_LABEL_ROWS - 1 && xpx < (rowEnd[row] ?? -Infinity) + gap) row++;
    rowEnd[row] = xpx + widthOf(m.text);
    out.push({ ...m, row });
  }
  return out;
}
