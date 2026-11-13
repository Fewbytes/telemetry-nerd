import type { BucketStatePayload } from "../lib/api";
import type { FleetData } from "./fleet";

/** Group density cloud (spec 2026-10-02 §7.1): per step, the share of the members alive at that
 * step in each value bin. A column sums to reporting / alive, so silent members thin it. */
export interface FleetCloud {
  edges: number[]; log: boolean; density: number[][];
  silent_n: number[]; silent_members: { id: string; steps: number }[]; silent_more: number;
  /** the group's merged bucket_state (merge(), spec §5.2) with the member counts behind each cell */
  rug?: BucketStatePayload & { alive: number[]; reporting: number[] };
}

export interface CloudGrid { cols: number; bins: number; share: Float32Array; silent: Uint16Array; max: number }

/** Average steps into at most `maxCols` pixel columns (bins rows each, row 0 = lowest value).
 * Averaging keeps the share meaning "members at this value / alive"; a column's silent count is its
 * largest, so a short silence is never averaged away. */
export function cloudGrid(c: FleetCloud, maxCols: number): CloudGrid {
  const steps = c.density.length, bins = c.edges.length - 1;
  const cols = Math.max(1, Math.min(steps, maxCols));
  const share = new Float32Array(cols * bins);
  const silent = new Uint16Array(cols);
  let max = 0;
  for (let k = 0; k < cols; k++) {
    const t0 = Math.floor((k * steps) / cols), t1 = Math.max(t0 + 1, Math.floor(((k + 1) * steps) / cols));
    for (let t = t0; t < t1; t++) {
      for (let b = 0; b < bins; b++) share[k * bins + b] += c.density[t][b] / (t1 - t0);
      silent[k] = Math.max(silent[k], c.silent_n[t]);
    }
    for (let b = 0; b < bins; b++) max = Math.max(max, share[k * bins + b]);
  }
  return { cols, bins, share, silent, max };
}

export function cloudLegendText(c: FleetCloud, members: number): string {
  const named = c.silent_members.map((m) => `${m.id} (${m.steps} steps)`).join(", ");
  const silent = c.silent_members.length
    ? `silent while alive: ${named}${c.silent_more ? ` and ${c.silent_more} more` : ""}`
    : "no member was silent while alive";
  return `${members} members · shade: share of the members alive at that step that sit in that value range (a column's shade adds up to reporting / alive, so silent members thin it) · `
    + `${silent} · bottom strip: the group's merged coverage (grey: members silent, hatched: unknown)`;
}

export function cloudTip(d: FleetData, c: FleetCloud, j: number, b: number, at: string): string {
  const lo = c.edges[b], hi = c.edges[b + 1];
  const pct = Math.round(c.density[j][b] * 100);
  const alive = d.alive[j], silent = c.silent_n[j];
  return `${at} · ${Number(lo.toPrecision(4))} – ${Number(hi.toPrecision(4))}: ${pct}% of ${alive} alive members${silent ? ` · ${silent} silent` : ""}`;
}
