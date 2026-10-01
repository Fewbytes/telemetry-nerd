// ui/src/chart/indexed.ts — indexed view (bead 4ok.14): series ÷ common baseline, log axis, 1 centred.
import type { IndexPayload, SeriesData } from "../lib/api";
import { seriesName } from "./toUplot";

export interface Indexed { series: SeriesData[]; skipped: string[]; hidden: number; nonPositive: number; refused: string | null }

export function indexSeries(series: SeriesData[], ix: IndexPayload, o: { quantile: boolean; nMin: number | null }): Indexed {
  const out: Indexed = { series: [], skipped: [], hidden: 0, nonPositive: 0, refused: ix.refused ?? null };
  if (out.refused) return out;
  const refs = new Map((ix.series ?? []).map((r) => [r.id, new Map(r.ts.map((t, i) => [t, i]))]));
  const refById = new Map((ix.series ?? []).map((r) => [r.id, r]));
  for (const s of series) {
    let base: (i: number) => number | null;
    if (ix.values) {
      const b = ix.values[s.id];
      if (b == null || !(b > 0)) { out.skipped.push(seriesName(s.labels)); continue; }
      base = () => b;
    } else {
      const r = refById.get(s.id), at = refs.get(s.id);
      if (!r || !at) { out.skipped.push(seriesName(s.labels)); continue; }
      base = (i) => {
        const j = at.get(s.ts[i]);
        if (j === undefined) return null;
        const v = r.avg[j], n = r.count[j] ?? 0;
        return v !== null && v > 0 && (!o.quantile || o.nMin === null || n >= o.nMin) ? v : null;
      };
    }
    const div = (arr: (number | null)[], count: boolean) => arr.map((v, i) => {
      if (v === null) return null;
      const b = base(i);
      if (b === null) { if (count) out.hidden++; return null; }
      const r = v / b;
      if (!(r > 0)) { if (count) out.nonPositive++; return null; }
      return r;
    });
    out.series.push({ ...s, avg: div(s.avg, true), min: div(s.min, false), max: div(s.max, false) });
  }
  if (!out.series.length) out.refused = "no series has a baseline > 0 (missing, zero or negative)";
  return out;
}

const NICE = [1.1, 1.25, 1.5, 2, 3, 5, 10, 20, 50, 100, 1000];
export function ratioRange(values: number[]): [number, number] {
  const m = Math.max(1, ...values.filter((v) => v > 0).map((v) => Math.max(v, 1 / v))) * 1.02;
  const n = NICE.find((x) => x >= m) ?? 10 ** Math.ceil(Math.log10(m));
  return [1 / n, n];
}
export function ratioTicks(n: number): number[] {
  if (n >= 10) { // wide range: decades, symmetric around 1
    const d = Math.floor(Math.log10(n * 1.0001));
    const ups = Array.from({ length: d }, (_, i) => 10 ** (i + 1));
    return [...ups.map((x) => 1 / x).reverse(), 1, ...ups];
  }
  const up = NICE.filter((x) => x <= n * 1.0001).slice(-3);
  return [...up.map((x) => 1 / x).reverse(), 1, ...up];
}
export const fmtRatio = (r: number): string => `×${Number(r.toPrecision(r >= 1 ? 3 : 2))}`;
