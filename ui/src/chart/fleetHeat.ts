import type { FleetData, FleetHeat, FleetHeatRow } from "./fleet";

/** Member x time heatmap of z and small-multiple helpers for the fleet panel (bead lkn.11). */

// ColorBrewer PuOr: diverging, colour-blind safe (blue-content vs orange, no red-green). Orange is
// above the other members, purple below. Light theme: neutral grey centre, darker toward the ends.
// Dark theme: dark neutral centre, lighter toward the ends, so deviation is always "more contrast".
type Mode = "light" | "dark";
export const DIVERGING: Record<Mode, string[]> = {
  light: ["#542788", "#8061a8", "#b2abd2", "#e6e6e6", "#f1a340", "#d97a14", "#b35806"],
  dark: ["#d4c9ff", "#9a86e0", "#5b4a9c", "#2f333b", "#a8631a", "#e08a2c", "#ffc880"],
};
// absent anchors are ordered low -> high; the neutral midpoint sits at index 3 of 7
const Z_CAP = 6;
const MIN_ROW_PX = 4; // 2 px per column x 4 px per row is the heatmap budget's cell
const MAX_ROW_PX = 14;
const HEAT_TARGET_PX = 420;

const hex = (h: string): number[] => {
  const n = parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};

/** 256-entry lookup: index 0 = -cap, 255 = +cap, RGB triples. */
export function divergingLut(mode: Mode): Uint8ClampedArray {
  const pts = DIVERGING[mode].map(hex);
  const out = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const x = (i / 255) * (pts.length - 1);
    const j = Math.min(pts.length - 2, Math.floor(x)), f = x - j;
    for (let c = 0; c < 3; c++) out[i * 3 + c] = Math.round(pts[j][c] + (pts[j + 1][c] - pts[j][c]) * f);
  }
  return out;
}

export const lutIndex = (z: number, cap = Z_CAP): number =>
  Math.max(0, Math.min(255, Math.round(((Math.max(-cap, Math.min(cap, z)) / cap + 1) / 2) * 255)));

export const GAP = 1, ABSENT = 2;
export interface HeatGrid {
  rows: number; bins: number; cols: number;
  z: Float32Array; // rows x bins, NaN where no value
  kind: Uint8Array; // rows x bins: 0 value, GAP silent while alive, ABSENT outside the member's lifetime
}

/**
 * Bin time columns to at most `maxBins`; a bin shows its most extreme z (so a spike is never
 * averaged away). A bin with no value is a GAP unless it lies wholly before the member's first or
 * after its last report (ABSENT).
 */
export function binHeat(h: FleetHeat, cols: number, maxBins: number): HeatGrid {
  const bins = Math.max(1, Math.min(cols, maxBins));
  const rows = h.rows.length;
  const z = new Float32Array(rows * bins).fill(NaN);
  const kind = new Uint8Array(rows * bins);
  h.rows.forEach((r, i) => {
    for (let b = 0; b < bins; b++) {
      const c0 = Math.floor((b * cols) / bins), c1 = Math.max(c0 + 1, Math.floor(((b + 1) * cols) / bins));
      let best = NaN;
      for (let c = c0; c < c1; c++) {
        const v = r.z[c];
        if (v !== null && v !== undefined && (Number.isNaN(best) || Math.abs(v) > Math.abs(best))) best = v;
      }
      const o = i * bins + b;
      z[o] = best;
      if (Number.isNaN(best)) kind[o] = r.first < 0 || c1 - 1 < r.first || c0 > r.last ? ABSENT : GAP;
    }
  });
  return { rows, bins, cols, z, kind };
}

export function rowPx(rows: number): number {
  return Math.max(MIN_ROW_PX, Math.min(MAX_ROW_PX, Math.floor(HEAT_TARGET_PX / Math.max(rows, 1))));
}

/** Label y positions (px) for outlier rows: centred on the row, pushed down to keep `gap` between labels. */
export function labelYs(rowsAt: number[], rowH: number, gap = 12): number[] {
  const out: number[] = [];
  let prev = -Infinity;
  for (const r of rowsAt) {
    const y = Math.max(r * rowH + rowH / 2, prev + gap, gap / 2);
    out.push(y);
    prev = y;
  }
  return out;
}

export function heatLegendText(h: FleetHeat, shownRows: number): string {
  const rows = shownRows < h.rows_total ? `${shownRows} of ${h.rows_total} members (most deviating shown)` : `${h.rows_total} members`;
  return `${rows} · colour: how far a member is from the others at that step, in the fleet's robust sigma (capped at ±${h.z_cap}); `
    + "orange above, purple below · dots: silent while alive · blank: before first / after last report · "
    + "each pixel column shows its most extreme step · rows: higher outliers first, then by median, lower outliers last";
}

/** The colour bar's labels: centred on 0, named for what the colour is (never a value histogram). */
export function heatBarLabels(cap: number): { title: string; lo: string; mid: string; hi: string } {
  return { title: "deviation from fleet median (σ)", lo: `−${cap}`, mid: "0", hi: `+${cap}` };
}

export function heatTip(r: FleetHeatRow, z: number | null | undefined, at: string): string {
  if (z === null || z === undefined) return `${r.id} · ${at} · no report`;
  return `${r.id} · ${at} · z ${z > 0 ? "+" : ""}${z.toFixed(1)}`;
}

const LINES = new Set(["outlier", "muted", "episode"]);
const lowEdge = (r: string) => r === "lo" || r === "lo3" || r === "lo2" || r === "thrlo";
const highEdge = (r: string) => r === "hi" || r === "hi3" || r === "hi2" || r === "thrhi";

/** Decimate fleet columns to <= maxPts points: band edges keep min/max, an outlier keeps its farthest point from the median. */
export function decimate(cols: (number | null)[][], roles: string[], maxPts: number): (number | null)[][] {
  const n = cols[0].length;
  if (n <= maxPts) return cols;
  const k = Math.ceil(n / maxPts);
  const bins = Math.ceil(n / k);
  // the centre an outlier's deviation is measured from: the fleet median, else the first group's
  const medIdx = roles.indexOf("median") >= 0 ? roles.indexOf("median") : roles.indexOf("cmedian");
  return cols.map((col, c) => {
    const role = roles[c];
    const out: (number | null)[] = [];
    for (let b = 0; b < bins; b++) {
      const lo = b * k, hi = Math.min(n, lo + k);
      if (role === "x") { out.push(col[lo]); continue; }
      let pick: number | null = null, sum = 0, cnt = 0, bestDev = -1;
      for (let i = lo; i < hi; i++) {
        const v = col[i];
        if (v === null) continue;
        sum += v; cnt++;
        if (lowEdge(role)) pick = pick === null ? v : Math.min(pick, v);
        else if (highEdge(role)) pick = pick === null ? v : Math.max(pick, v);
        else if (LINES.has(role)) {
          const m = medIdx >= 0 ? cols[medIdx][i] : null;
          const dev = m === null ? 0 : Math.abs(v - m);
          if (dev > bestDev) { bestDev = dev; pick = v; }
        }
      }
      out.push(lowEdge(role) || highEdge(role) || LINES.has(role) ? pick : cnt ? sum / cnt : null);
    }
    return out;
  });
}

/** One y range for every small multiple: the fleet's min-max envelope and every drawn outlier. */
export function sharedRange(d: FleetData, upTo = d.outliers.length): [number, number] {
  let lo = Infinity, hi = -Infinity;
  const take = (a: (number | null)[]) => a.forEach((v) => { if (v !== null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
  take(d.band.lo); take(d.band.hi);
  d.outliers.slice(0, upTo).forEach((o) => take(o.values));
  if (!Number.isFinite(lo)) return [0, 1];
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.05;
  return [lo - pad, hi + pad];
}
