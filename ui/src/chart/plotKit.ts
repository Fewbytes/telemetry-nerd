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

/** A tooltip's position inside its wrapper (CSS px); `flip` means it sits left of the pointer. */
export interface HoverTip { x: number; y: number; text: string; flip: boolean }

/**
 * Place a tooltip 12px below-right of the cursor (below-left on the wrapper's right half) from a
 * uPlot cursor position (`left`/`top`, relative to the plot area).
 */
export function tipAt(p: uPlot, wrap: HTMLElement, left: number, top: number, text: string): HoverTip {
  const o = p.over.getBoundingClientRect(), w = wrap.getBoundingClientRect();
  const x = o.left - w.left + left, y = o.top - w.top + top;
  const flip = x > w.width / 2;
  return { x: flip ? x - 12 : x + 12, y: y + 12, text, flip };
}
