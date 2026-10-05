import type { SeriesData } from "../lib/api";

/** What the range selector's in-place preview chart draws, regardless of which tier produced
 * it: a client-side slice of data already on screen (`source: "local"`, bead geje tier 1 — no
 * round trip), or a dataset the server fetched for a range outside it (`source: "fetched"`,
 * tier 2). Both render through the same path so "preview, not this panel's evidence" always
 * looks the same. */
export interface PreviewRender {
  series: SeriesData[];
  start_ms: number;
  end_ms: number;
  step_ms: number;
  source: "local" | "fetched";
}

const pick = <T,>(col: T[] | undefined, keep: number[]): T[] | undefined =>
  col ? keep.map((i) => col[i]) : undefined;

/** Narrow already-fetched series to [start_ms, end_ms] (inclusive), client-side: the in-bounds
 * tier of the range selector needs no server round trip, just a window of what's already in
 * hand. */
export function sliceSeries(series: SeriesData[], start_ms: number, end_ms: number): SeriesData[] {
  return series.map((s) => {
    const keep: number[] = [];
    s.ts.forEach((t, i) => {
      if (t >= start_ms && t <= end_ms) keep.push(i);
    });
    return {
      ...s,
      ts: keep.map((i) => s.ts[i]),
      avg: pick(s.avg, keep)!,
      min: pick(s.min, keep)!,
      max: pick(s.max, keep)!,
      count: pick(s.count, keep)!,
      lo: pick(s.lo, keep),
      hi: pick(s.hi, keep),
    };
  });
}
