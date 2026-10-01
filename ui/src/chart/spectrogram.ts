import { cellSpan, valueAxis, type ValueAxis, type Span } from "./heatmap";

export interface SpectroSeries {
  id: string; labels: Record<string, string>; ts: number[];
  rows: { lo_s: number[]; hi_s: number[] }; power: ((number | null)[] | null)[]; level: (number | null)[];
}
export interface SpectroRect { x: number; y: number; w: number; h: number; t: number; faint: boolean; col: number; row: number }

/** Columns are windows centred on `ts`; rows are log-period bins (long periods up). Power is linear 0..1. */
export function layoutSpectrogram(s: SpectroSeries, o: { width: number; height: number; startMs: number; endMs: number; hopMs: number }) {
  const axis: ValueAxis = valueAxis(s.rows.lo_s, s.rows.hi_s, o.height, "log");
  const toX = (ms: number) => ((ms - o.startMs) * o.width) / (o.endMs - o.startMs);
  const rects: SpectroRect[] = [], missing: Span[] = [];
  s.ts.forEach((c, col) => {
    const x = toX(c - o.hopMs / 2), w = toX(c + o.hopMs / 2) - x;
    const colPower = s.power.map((r) => (r ? r[col] : null));
    if (colPower.every((v) => v === null)) { missing.push({ x, w }); return; }
    colPower.forEach((v, row) => {
      if (v === null) return;
      const [p0, p1] = cellSpan(axis, s.rows.lo_s[row], s.rows.hi_s[row]);
      rects.push({ x, w, y: o.height - p1, h: p1 - p0, t: v, faint: v < (s.level[col] ?? 1), col, row });
    });
  });
  return { axis, rects, missing };
}
