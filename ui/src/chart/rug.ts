import type { BucketStatePayload } from "../lib/api";
import { fmtRange, fmtStep } from "../lib/format";

/** bucket_state codes (src/telemetry_nerd/model/bucket_state.py). */
export const STATE = { OK: 0, PARTIAL: 1, EMPTY: 2, ABSENT: 3, UNKNOWN: 4 } as const;
export const ROW_H = 8;
export const ROW_GAP = 2;
export const MAX_ROWS = 5;

export interface RugCell { row: number; x: number; w: number; state: number; missing: number; ts: number; i: number }
export interface RugStyle { tint: (row: number) => string; grey: string; line: string }

export const rugHeight = (rows: number): number => (rows === 0 ? 0 : Math.min(rows, MAX_ROWS) * (ROW_H + ROW_GAP) + 2);

/** CSS px between the plot floor and the top of the rug, so it never merges with a line at y=0. */
export const RUG_GAP = 3;

/** Extra x-axis space (CSS px) the rug claims between plot floor and tick labels: 0 when nothing is drawn. */
export const rugAxisExtra = (rows: number): number => (rows === 0 ? 0 : rugHeight(rows) + RUG_GAP);

/** Top (CSS px) of stacked facet i for the brush overlay: each facet is its plot height plus the
 * label row (14) and count strip (30), plus the rug band when that facet draws a rug. */
export const facetTop = (hasRug: boolean[], i: number, facetH: number): number =>
  hasRug.slice(0, i).reduce((top, rug) => top + facetH + 14 + 30 + (rug ? rugAxisExtra(1) : 0), 0) + 4;

/** Top of the rug canvas (CSS px, same frame as the plot-bottom it is given). */
export const rugTop = (plotBottom: number): number => plotBottom + RUG_GAP;

/** One cell per bucket per row; bucket ts is the bucket END, so a cell spans (ts - step, ts]. */
export function rugCells(states: BucketStatePayload[], stepMs: number, toX: (ms: number) => number): RugCell[] {
  const out: RugCell[] = [];
  states.slice(0, MAX_ROWS).forEach((s, row) => {
    s.ts.forEach((t, i) => {
      const x0 = toX(t - stepMs), x1 = toX(t);
      const exp = s.expected[i] || 1;
      const missing = Math.max(0, Math.min(1, 1 - s.observed[i] / exp));
      out.push({ row, x: x0, w: Math.max(1, x1 - x0), state: s.state[i], missing, ts: t, i });
    });
  });
  return out;
}

const rowY = (row: number) => row * (ROW_H + ROW_GAP) + 1;

function hatch(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
  ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip(); ctx.beginPath();
  for (let d = -h; d < w; d += 4) { ctx.moveTo(x + d, y + h); ctx.lineTo(x + d + h, y); }
  ctx.stroke(); ctx.restore();
}

export function drawRug(ctx: CanvasRenderingContext2D, cells: RugCell[], style: RugStyle): void {
  ctx.lineWidth = 1;
  ctx.strokeStyle = style.line;
  for (const c of cells) {
    const y = rowY(c.row);
    if (c.state === STATE.OK || c.state === STATE.PARTIAL) {
      ctx.fillStyle = style.tint(c.row);
      ctx.fillRect(c.x, y, c.w, ROW_H);
      if (c.state === STATE.PARTIAL) {
        // floor keeps small shortfalls visibly different from ok
        ctx.globalAlpha = Math.max(0.25, c.missing);
        ctx.fillStyle = style.grey;
        ctx.fillRect(c.x, y, c.w, ROW_H);
        ctx.globalAlpha = 1;
      }
    } else if (c.state === STATE.EMPTY) {
      ctx.fillStyle = style.grey;
      ctx.fillRect(c.x, y, c.w, ROW_H);
    } else if (c.state === STATE.ABSENT) {
      ctx.save(); ctx.setLineDash([1, 2]); ctx.beginPath();
      ctx.moveTo(c.x, y + ROW_H / 2); ctx.lineTo(c.x + c.w, y + ROW_H / 2); ctx.stroke(); ctx.restore();
    } else {
      hatch(ctx, c.x, y, c.w, ROW_H);
    }
  }
}

export function hitRug(cells: RugCell[], px: number, py: number): RugCell | null {
  for (const c of cells) {
    const y = rowY(c.row);
    if (px >= c.x && px < c.x + c.w && py >= y && py < y + ROW_H + ROW_GAP) return c;
  }
  return null;
}

const WORDS: Record<number, string> = {
  [STATE.OK]: "ok", [STATE.PARTIAL]: "fewer samples than expected", [STATE.EMPTY]: "no samples",
  [STATE.ABSENT]: "series not seen yet", [STATE.UNKNOWN]: "unknown (fetch failed or source could not tell)",
};

export const FLAG_SOURCE_FILLED = 8;
export const FLAG_POST_GAP = 16;

/** `samples`: the state counts scrape samples (expected = the series' own rate in this bucket);
 * otherwise it only records presence (quantiles, heatmap columns) and has no sample count. */
export function rugHint(cell: RugCell, s: BucketStatePayload, stepMs: number, name: string, samples: boolean): string {
  const obs = s.observed[cell.i], exp = s.expected[cell.i];
  const lines = [`${name}`, `${fmtRange(cell.ts - stepMs, cell.ts)} · ${WORDS[cell.state] ?? cell.state}`];
  if (samples && exp > 0 && cell.state !== STATE.ABSENT && cell.state !== STATE.UNKNOWN) {
    const every = Math.max(1000, Math.round(stepMs / exp / 1000) * 1000);
    lines.push(`${obs} of ${Math.round(exp)} expected samples (series reports every ${fmtStep(every)})`);
  }
  const flags = s.flags[cell.i] ?? 0;
  if (flags & FLAG_SOURCE_FILLED) lines.push("coverage cannot be observed for this expression");
  if (flags & FLAG_POST_GAP) lines.push("computed from the sample before the gap (VictoriaMetrics); not a real spike");
  const seen = s.ts.filter((t, k) => t <= cell.ts && s.observed[k] > 0).at(-1);
  if (cell.state === STATE.EMPTY && seen !== undefined) lines.push(`last seen in bucket ending ${fmtRange(seen - stepMs, seen)}`);
  return lines.join("\n");
}

/** Label under the rug when the server left out series that also have non-ok coverage. */
export const rugMoreLabel = (more: number): string =>
  more > 0 ? `+${more} more series with coverage issues (see footer)` : "";
