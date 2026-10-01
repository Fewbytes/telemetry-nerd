// ui/src/chart/marginal.ts — marginal histogram beside a time panel (spec §6.4, bead 4ok.6).
// Pure layout + a thin canvas draw. Bars are whole source buckets (or fine sample bins), merged
// only by whole adjacent buckets; length = share per px of y (density), so windows compare.
import type { WindowHist } from "../lib/api";

export interface MBar { y0: number; y1: number; share: number; density: number }
export interface MLayout { bars: MBar[]; above: number; below: number; n: number }

export function marginalBars(w: WindowHist, toPx: (v: number) => number, top: number, bottom: number, minPx = 3): MLayout {
  const t = w.c.reduce((a, b) => a + b, 0);
  const out: MLayout = { bars: [], above: 0, below: 0, n: w.n };
  if (!(t > 0)) return out;
  const idx = w.c.map((_, i) => i).sort((i, j) => (w.lo[i] ?? -Infinity) - (w.lo[j] ?? -Infinity));
  let acc: { top: number; bot: number; c: number } | null = null;
  const flush = () => {
    if (acc && acc.c > 0) {
      const share = acc.c / t;
      out.bars.push({ y0: Math.max(top, acc.top), y1: Math.min(bottom, acc.bot), share, density: share / (acc.bot - acc.top) });
    }
    acc = null;
  };
  for (const i of idx) {
    const c = w.c[i], lo = w.lo[i], hi = w.hi[i];
    if (hi === null) { out.above += c / t; continue; } // (e, +Inf): no range to draw
    if (lo === null) { out.below += c / t; continue; }
    const pLo = toPx(lo), pHi = toPx(hi); // screen y grows downward: pHi < pLo
    if (!Number.isFinite(pLo) || !Number.isFinite(pHi) || pHi >= bottom) { flush(); out.below += c / t; continue; }
    if (pLo <= top) { flush(); out.above += c / t; continue; }
    if (acc && acc.bot - acc.top < minPx) { acc.top = pHi; acc.c += c; continue; }
    flush();
    acc = { top: pHi, bot: pLo, c };
  }
  flush();
  return out;
}

/** One length scale for every window: the largest density fills `width`. */
export function scaleBars(layouts: MLayout[], width: number): number[][] {
  const max = Math.max(0, ...layouts.flatMap((l) => l.bars.map((b) => b.density)));
  return layouts.map((l) => l.bars.map((b) => (max > 0 ? (b.density / max) * width : 0)));
}

const fmtN = (n: number) => (n >= 1000 ? `${Number((n / 1000).toPrecision(2))}k` : `${Math.round(n)}`);
export const marginalHeader = (ws: WindowHist[], nMin: number): string[] =>
  ws.map((w) => `${w.label} n=${fmtN(w.n)}${w.n < nMin ? " (too few)" : ""}`);

export function drawMarginal(
  ctx: CanvasRenderingContext2D, ws: WindowHist[], nMin: number,
  o: { toPx: (v: number) => number; top: number; bottom: number; width: number; fg: string; muted: string },
): void {
  const layouts = ws.map((w) => marginalBars(w, o.toPx, o.top, o.bottom));
  const lens = scaleBars(layouts, o.width - 6);
  layouts.forEach((l, k) => {
    ctx.globalAlpha = ws[k].n < nMin ? 0.35 : 1;
    if (k === 0) { // now: filled
      ctx.fillStyle = o.fg;
      l.bars.forEach((b, i) => { ctx.globalAlpha *= 0.5; ctx.fillRect(2, b.y0, lens[k][i], b.y1 - b.y0); ctx.globalAlpha = ws[k].n < nMin ? 0.35 : 1; });
    } else { // reference: dashed outline steps
      ctx.strokeStyle = o.muted; ctx.setLineDash([3, 2]); ctx.beginPath();
      l.bars.forEach((b, i) => { ctx.moveTo(2, b.y1); ctx.lineTo(2 + lens[k][i], b.y1); ctx.lineTo(2 + lens[k][i], b.y0); ctx.lineTo(2, b.y0); });
      ctx.stroke(); ctx.setLineDash([]);
    }
    ctx.fillStyle = k === 0 ? o.fg : o.muted; ctx.font = "10px sans-serif"; ctx.globalAlpha = 1;
    if (l.above > 0) ctx.fillText(`▲≥${(100 * l.above).toPrecision(2)}%`, 2, o.top + 10 + 11 * k);
    if (l.below > 0) ctx.fillText(`▼≥${(100 * l.below).toPrecision(2)}%`, 2, o.bottom - 2 - 11 * k);
  });
}
