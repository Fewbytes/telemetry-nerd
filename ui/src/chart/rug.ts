import type { BucketStatePayload } from "../lib/api";
import { fmtRange } from "../lib/format";

/** bucket_state codes (src/telemetry_nerd/model/bucket_state.py). */
export const STATE = { OK: 0, PARTIAL: 1, EMPTY: 2, ABSENT: 3, UNKNOWN: 4 } as const;
export const ROW_H = 8;
export const ROW_GAP = 2;
export const MAX_ROWS = 5;

export interface RugCell { row: number; x: number; w: number; state: number; missing: number; ts: number; i: number }
export interface RugStyle { tint: (row: number) => string; grey: string; line: string }

export const rugHeight = (rows: number): number => (rows === 0 ? 0 : Math.min(rows, MAX_ROWS) * (ROW_H + ROW_GAP) + 2);

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

export function rugHint(cell: RugCell, s: BucketStatePayload, stepMs: number, name: string, resolutionMs: number): string {
  const obs = s.observed[cell.i], exp = s.expected[cell.i];
  const lines = [`${name}`, `${fmtRange(cell.ts - stepMs, cell.ts)} · ${WORDS[cell.state] ?? cell.state}`];
  if (cell.state !== STATE.ABSENT && cell.state !== STATE.UNKNOWN)
    lines.push(`${obs} of ${exp} expected samples (every ${resolutionMs / 1000}s)`);
  const seen = s.ts.filter((t, k) => t <= cell.ts && s.observed[k] > 0).at(-1);
  if (cell.state === STATE.EMPTY && seen !== undefined) lines.push(`last seen in bucket ending ${fmtRange(seen - stepMs, seen)}`);
  return lines.join("\n");
}
