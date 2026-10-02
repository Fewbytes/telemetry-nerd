import type uPlot from "uplot";

/** A series that is drawn nowhere: it exists only so a band can reference its column. */
export const HIDDEN_SERIES: uPlot.Series = { stroke: "transparent", width: 0, points: { show: false } };

/** x and y axes in the panel's muted stroke and grid colours; `y` / `x` add per-axis options. */
export function plotAxes(stroke: string, grid: string, y: uPlot.Axis = {}, x: uPlot.Axis = {}): uPlot.Axis[] {
  const base = { stroke, grid: { stroke: grid }, ticks: { stroke: grid } };
  return [{ ...base, ...x }, { ...base, ...y }];
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
