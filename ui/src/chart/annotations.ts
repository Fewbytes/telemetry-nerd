import type uPlot from "uplot";
import type { Annotation } from "../lib/api";

// uPlot x units are seconds; annotation timestamps are epoch ms.
const MS = 1000;

export type DrawOp =
  | { type: "vline"; x: number; label: string; author: string }
  | { type: "xband"; x0: number; x1: number; label: string; author: string }
  | { type: "hline"; y: number; label: string; author: string }
  | { type: "yband"; y0: number; y1: number; label: string; author: string };

export interface ScaleRange {
  min: number;
  max: number;
}

// event → dashed vline, region → translucent xband, threshold → hline,
// band → translucent yband. Notes carry no geometry, so they draw nothing.
// Ops fully outside the visible scales are dropped; partial ones are clamped.
export function drawOps(anns: Annotation[], x: ScaleRange, y: ScaleRange): DrawOp[] {
  const ops: DrawOp[] = [];
  for (const a of anns) {
    switch (a.kind) {
      case "event": {
        if (a.t_start_ms === null) break;
        const px = a.t_start_ms / MS;
        if (px < x.min || px > x.max) break;
        ops.push({ type: "vline", x: px, label: a.label, author: a.author });
        break;
      }
      case "region": {
        if (a.t_start_ms === null || a.t_end_ms === null) break;
        const x0 = a.t_start_ms / MS;
        const x1 = a.t_end_ms / MS;
        if (x1 <= x.min || x0 >= x.max) break;
        ops.push({
          type: "xband",
          x0: Math.max(x0, x.min),
          x1: Math.min(x1, x.max),
          label: a.label,
          author: a.author,
        });
        break;
      }
      case "threshold": {
        if (a.value === null) break;
        if (a.value < y.min || a.value > y.max) break;
        ops.push({ type: "hline", y: a.value, label: a.label, author: a.author });
        break;
      }
      case "band": {
        if (a.value === null || a.value_hi === null) break;
        if (a.value_hi <= y.min || a.value >= y.max) break;
        ops.push({
          type: "yband",
          y0: Math.max(a.value, y.min),
          y1: Math.min(a.value_hi, y.max),
          label: a.label,
          author: a.author,
        });
        break;
      }
      case "note":
        break;
    }
  }
  return ops;
}

export interface AnnotationColors {
  claude: string;
  user: string;
  other: string;
}

const FALLBACK_COLORS: AnnotationColors = { claude: "#8250df", user: "#e69f00", other: "#808080" };

export const authorColor = (author: string, colors: AnnotationColors): string =>
  author === "claude" ? colors.claude : author === "user" ? colors.user : colors.other;

export function readAnnotationColors(el: HTMLElement): AnnotationColors {
  const cs = getComputedStyle(el);
  const token = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return {
    claude: token("--ann-claude", FALLBACK_COLORS.claude),
    user: token("--ann-user", FALLBACK_COLORS.user),
    other: FALLBACK_COLORS.other,
  };
}

// Structural slice of uPlot we draw against; valToPos with canvasPixels=true
// and bbox are both in canvas pixels, matching uPlot's own overlay pattern.
export interface PlotLike {
  ctx: CanvasRenderingContext2D;
  bbox: uPlot.BBox;
  valToPos(val: number, scaleKey: string, canvasPixels?: boolean): number;
}

export function drawAnnotations(
  plot: PlotLike,
  ops: DrawOp[],
  colors: AnnotationColors,
  dpr = 1,
): void {
  const { ctx, bbox: b } = plot;
  const pad = 4 * dpr;
  ctx.save();
  ctx.beginPath();
  ctx.rect(b.left, b.top, b.width, b.height);
  ctx.clip();
  ctx.font = `${Math.round(12 * dpr)}px system-ui, sans-serif`;
  ctx.textBaseline = "top";

  const label = (text: string, color: string, x: number, y: number) => {
    if (!text) return;
    ctx.fillStyle = color;
    const w = ctx.measureText(text).width;
    ctx.fillText(text, Math.min(x, b.left + b.width - w - pad), Math.max(b.top + pad, y));
  };
  const fill = (color: string, x: number, y: number, w: number, h: number) => {
    ctx.save();
    ctx.globalAlpha = 0.18;
    ctx.fillStyle = color;
    ctx.fillRect(x, y, w, h);
    ctx.restore();
  };
  const line = (color: string, x0: number, y0: number, x1: number, y1: number, dash: number[]) => {
    ctx.save();
    ctx.setLineDash(dash);
    ctx.strokeStyle = color;
    ctx.lineWidth = Math.max(1, 1.5 * dpr);
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
    ctx.restore();
  };

  for (const op of ops) {
    const color = authorColor(op.author, colors);
    if (op.type === "vline") {
      const px = plot.valToPos(op.x, "x", true);
      line(color, px, b.top, px, b.top + b.height, [6 * dpr, 4 * dpr]);
      label(op.label, color, px + pad, b.top + pad);
    } else if (op.type === "xband") {
      const x0 = plot.valToPos(op.x0, "x", true);
      const x1 = plot.valToPos(op.x1, "x", true);
      fill(color, x0, b.top, x1 - x0, b.height);
      label(op.label, color, x0 + pad, b.top + pad);
    } else if (op.type === "hline") {
      const py = plot.valToPos(op.y, "y", true);
      line(color, b.left, py, b.left + b.width, py, []);
      label(op.label, color, b.left + pad, py - 12 * dpr - pad);
    } else {
      const y0 = plot.valToPos(op.y0, "y", true);
      const y1 = plot.valToPos(op.y1, "y", true);
      const top = Math.min(y0, y1);
      fill(color, b.left, top, b.width, Math.abs(y1 - y0));
      label(op.label, color, b.left + pad, top + pad);
    }
  }
  ctx.restore();
}