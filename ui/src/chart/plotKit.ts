import type uPlot from "uplot";

/** A series that is drawn nowhere: it exists only so a band can reference its column. */
export const HIDDEN_SERIES: uPlot.Series = { stroke: "transparent", width: 0, points: { show: false } };

/** x and y axes in the panel's muted stroke and grid colours; `y` / `x` add per-axis options. */
export function plotAxes(stroke: string, grid: string, y: uPlot.Axis = {}, x: uPlot.Axis = {}): uPlot.Axis[] {
  const base = { stroke, grid: { stroke: grid }, ticks: { stroke: grid } };
  return [{ ...base, ...x }, { ...base, ...y }];
}

const AXIS_FONT_PX = 12;
const AXIS_FONT = '12px system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';

/**
 * A y-axis `size` that fits its longest formatted tick label, instead of uPlot's fixed 50px
 * default — the gutter that clips a unit-scaled label like "1.4 GB/s". uPlot's own canvas
 * (`self.ctx`) is device-pixel scaled, so the measured width is converted back to CSS pixels.
 */
type AxisSizeFn = Extract<uPlot.Axis.Size, (...args: never[]) => unknown>;

export function axisGutterSize(pad = 14, min = 40): AxisSizeFn {
  return (self, values) => {
    if (!values || !values.length) return min;
    const dpr = window.devicePixelRatio || 1;
    const ctx = self.ctx;
    ctx.save();
    ctx.font = AXIS_FONT.replace(`${AXIS_FONT_PX}px`, `${AXIS_FONT_PX * dpr}px`);
    const w = Math.max(...values.map((v) => ctx.measureText(v ?? "").width));
    ctx.restore();
    return Math.max(min, Math.ceil(w / dpr) + pad);
  };
}

/** Draw a dot per mark (filled, or hollow when `filled` says no); call from a uPlot draw hook. */
export function drawDots<M extends { x: number; y: number }>(
  p: uPlot, marks: M[], color: string, filled: (m: M) => boolean = () => true,
): void {
  const c = p.ctx, dpr = window.devicePixelRatio || 1;
  c.save();
  c.lineWidth = 1.5 * dpr;
  c.strokeStyle = color; c.fillStyle = color;
  for (const m of marks) {
    const x = p.valToPos(m.x, "x", true), y = p.valToPos(m.y, "y", true);
    c.beginPath(); c.arc(x, y, 3.5 * dpr, 0, 2 * Math.PI);
    if (filled(m)) c.fill(); else c.stroke();
  }
  c.restore();
}

/** A tooltip's position inside its wrapper (CSS px); `flip` means it sits left of the pointer, `flipY` above it. */
export interface HoverTip { x: number; y: number; text: string; flip: boolean; flipY: boolean }

/**
 * Place a tooltip 12px from a pointer at (x, y) inside a wrapper of size w x h: below-right by
 * default, flipping to the left on the wrapper's right half and above on its bottom half so it
 * grows toward the free space and never leaves the wrapper.
 */
export function placeTip(x: number, y: number, w: number, h: number, text: string): HoverTip {
  const flip = x > w / 2, flipY = y > h / 2;
  return { x: flip ? x - 12 : x + 12, y: flipY ? y - 12 : y + 12, text, flip, flipY };
}

/**
 * Final top-left of a tip of measured size tw x th inside a w x h wrapper: flipped tips end at the
 * pointer instead of starting there, then the box is pushed back inside (and pinned to the
 * top-left when it is bigger than the wrapper).
 */
export function clampTip(tip: Pick<HoverTip, "x" | "y" | "flip" | "flipY">, tw: number, th: number, w: number, h: number): { left: number; top: number } {
  const left = tip.flip ? tip.x - tw : tip.x, top = tip.flipY ? tip.y - th : tip.y;
  return { left: Math.max(0, Math.min(left, w - tw)), top: Math.max(0, Math.min(top, h - th)) };
}

/** `placeTip` from a uPlot cursor position (`left`/`top`, relative to the plot area). */
export function tipAt(p: uPlot, wrap: HTMLElement, left: number, top: number, text: string): HoverTip {
  const o = p.over.getBoundingClientRect(), w = wrap.getBoundingClientRect();
  return placeTip(o.left - w.left + left, o.top - w.top + top, w.width, w.height, text);
}

/** Hatch the x spans (seconds) over the plot's full height, clipped to the plot area. */
export function drawHatch(p: uPlot, spans: [number, number][], dark: boolean): void {
  if (!spans.length) return;
  const c = p.ctx, dpr = window.devicePixelRatio || 1;
  const { left, top, width, height } = p.bbox, gap = 7 * dpr;
  c.save();
  c.beginPath(); c.rect(left, top, width, height); c.clip();
  c.strokeStyle = dark ? "rgba(200,205,210,0.55)" : "rgba(80,85,90,0.5)"; c.lineWidth = 1 * dpr;
  for (const [s0, s1] of spans) {
    const x0 = Math.max(left, p.valToPos(s0, "x", true));
    const x1 = Math.min(left + width, p.valToPos(s1, "x", true));
    if (x1 <= x0) continue;
    c.save(); c.beginPath(); c.rect(x0, top, x1 - x0, height); c.clip();
    c.fillStyle = dark ? "rgba(22,24,29,0.35)" : "rgba(255,255,255,0.35)"; c.fillRect(x0, top, x1 - x0, height);
    c.beginPath();
    for (let x = x0 - height; x < x1; x += gap) { c.moveTo(x, top + height); c.lineTo(x + height, top); }
    c.stroke(); c.restore();
  }
  c.restore();
}
