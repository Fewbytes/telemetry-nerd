import { describe, expect, it } from "vitest";
import { authorColor, drawAnnotations, drawOps, type AnnotationColors } from "./annotations";
import type { Annotation } from "../lib/api";

const ann = (over: Partial<Annotation> = {}): Annotation => ({
  id: "a1", kind: "event", panel: "p1", t_start_ms: null, t_end_ms: null,
  value: null, value_hi: null, label: "L", links: [], author: "user",
  created_at_ms: 0, deleted: false, ...over,
});

const X = { min: 5, max: 10 }; // seconds
const Y = { min: 0, max: 1 };

describe("drawOps", () => {
  it("converts event timestamps ms → seconds into a vline", () => {
    expect(drawOps([ann({ kind: "event", t_start_ms: 5500, label: "deploy" })], X, Y)).toEqual([
      { type: "vline", x: 5.5, label: "deploy", author: "user" },
    ]);
  });

  it("converts a region into an xband clamped to the visible x scale", () => {
    expect(
      drawOps([ann({ kind: "region", t_start_ms: 4000, t_end_ms: 8000 })], X, Y),
    ).toEqual([{ type: "xband", x0: 5, x1: 8, label: "L", author: "user" }]);
    expect(
      drawOps([ann({ kind: "region", t_start_ms: 2000, t_end_ms: 7000 })], X, Y),
    ).toEqual([{ type: "xband", x0: 5, x1: 7, label: "L", author: "user" }]);
    expect(
      drawOps([ann({ kind: "region", t_start_ms: 6000, t_end_ms: 20000 })], X, Y),
    ).toEqual([{ type: "xband", x0: 6, x1: 10, label: "L", author: "user" }]);
  });

  it("keeps a fully visible region unchanged", () => {
    expect(
      drawOps([ann({ kind: "region", t_start_ms: 6000, t_end_ms: 7000 })], X, Y),
    ).toEqual([{ type: "xband", x0: 6, x1: 7, label: "L", author: "user" }]);
  });

  it("maps thresholds to an hline at the value (y is already data units)", () => {
    expect(drawOps([ann({ kind: "threshold", value: 0.5, author: "claude" })], X, Y)).toEqual([
      { type: "hline", y: 0.5, label: "L", author: "claude" },
    ]);
  });

  it("clamps a band to the visible y scale", () => {
    expect(
      drawOps([ann({ kind: "band", value: 0.2, value_hi: 1.5 })], X, Y),
    ).toEqual([{ type: "yband", y0: 0.2, y1: 1, label: "L", author: "user" }]);
    expect(
      drawOps([ann({ kind: "band", value: -1, value_hi: 0.4 })], X, Y),
    ).toEqual([{ type: "yband", y0: 0, y1: 0.4, label: "L", author: "user" }]);
  });

  it("drops ops fully outside the visible scales", () => {
    const anns = [
      ann({ id: "a1", kind: "event", t_start_ms: 1000 }),
      ann({ id: "a2", kind: "event", t_start_ms: 11000 }),
      ann({ id: "a3", kind: "region", t_start_ms: 1000, t_end_ms: 4000 }),
      ann({ id: "a4", kind: "region", t_start_ms: 11000, t_end_ms: 20000 }),
      ann({ id: "a5", kind: "threshold", value: 2 }),
      ann({ id: "a6", kind: "threshold", value: -1 }),
      ann({ id: "a7", kind: "band", value: 2, value_hi: 3 }),
      ann({ id: "a8", kind: "band", value: -2, value_hi: -1 }),
    ];
    expect(drawOps(anns, X, Y)).toEqual([]);
  });

  it("keeps an event exactly on a scale boundary", () => {
    expect(drawOps([ann({ kind: "event", t_start_ms: 5000 })], X, Y)).toHaveLength(1);
    expect(drawOps([ann({ kind: "event", t_start_ms: 10000 })], X, Y)).toHaveLength(1);
  });

  it("produces no ops for note annotations", () => {
    expect(drawOps([ann({ kind: "note", panel: "p1", label: "note text" })], X, Y)).toEqual([]);
  });
});

// recorder mock for the canvas 2D context
const mockCtx = () => {
  const calls: [string, ...unknown[]][] = [];
  const ctx = {
    save: () => calls.push(["save"]),
    restore: () => calls.push(["restore"]),
    beginPath: () => calls.push(["beginPath"]),
    rect: (x: number, y: number, w: number, h: number) => calls.push(["rect", x, y, w, h]),
    clip: () => calls.push(["clip"]),
    moveTo: (x: number, y: number) => calls.push(["moveTo", x, y]),
    lineTo: (x: number, y: number) => calls.push(["lineTo", x, y]),
    stroke: () => calls.push(["stroke"]),
    fillRect: (x: number, y: number, w: number, h: number) => calls.push(["fillRect", x, y, w, h]),
    fillText: (s: string, x: number, y: number) => calls.push(["fillText", s, x, y]),
    setLineDash: (d: number[]) => calls.push(["setLineDash", d]),
    measureText: (s: string) => ({ width: s.length * 6 } as TextMetrics),
  };
  return { ctx: ctx as unknown as CanvasRenderingContext2D, calls };
};

const mockPlot = (ctx: CanvasRenderingContext2D) => ({
  ctx,
  bbox: { left: 40, top: 10, width: 500, height: 200 },
  // x: 5..10s → 40..540px, y: 0..1 → 210..10px
  valToPos: (v: number, scale: string) => (scale === "x" ? 40 + (v - 5) * 100 : 210 - v * 200),
});

const COLORS: AnnotationColors = { claude: "#c1", user: "#u1", other: "#o1" };

describe("drawAnnotations", () => {
  it("draws events as dashed vertical lines with a label", () => {
    const { ctx, calls } = mockCtx();
    drawAnnotations(
      mockPlot(ctx),
      [{ type: "vline", x: 6, label: "deploy", author: "user" }],
      COLORS,
    );
    expect(calls).toContainEqual(["setLineDash", [6, 4]]);
    expect(calls).toContainEqual(["moveTo", 140, 10]);
    expect(calls).toContainEqual(["lineTo", 140, 210]);
    expect(calls).toContainEqual(["fillText", "deploy", 144, 14]);
  });

  it("draws regions as translucent fills and thresholds as solid hlines", () => {
    const { ctx, calls } = mockCtx();
    drawAnnotations(
      mockPlot(ctx),
      [
        { type: "xband", x0: 5, x1: 7, label: "reg", author: "claude" },
        { type: "hline", y: 0.5, label: "thr", author: "claude" },
        { type: "yband", y0: 0.2, y1: 0.8, label: "band", author: "user" },
      ],
      COLORS,
    );
    expect(calls).toContainEqual(["fillRect", 40, 10, 200, 200]); // xband
    expect(calls).toContainEqual(["moveTo", 40, 110]); // hline
    expect(calls).toContainEqual(["lineTo", 540, 110]);
    expect(calls).toContainEqual(["fillRect", 40, 50, 500, 120]); // yband
    expect(calls).toContainEqual(["fillText", "band", 44, 54]);
  });

  it("clips drawing to the plot bbox and restores the context", () => {
    const { ctx, calls } = mockCtx();
    drawAnnotations(mockPlot(ctx), [], COLORS);
    expect(calls[0]).toEqual(["save"]);
    expect(calls[1]).toEqual(["beginPath"]);
    expect(calls[2]).toEqual(["rect", 40, 10, 500, 200]);
    expect(calls).toContainEqual(["clip"]);
    expect(calls[calls.length - 1]).toEqual(["restore"]);
  });
});

describe("authorColor", () => {
  it("picks the token color by author", () => {
    expect(authorColor("claude", COLORS)).toBe("#c1");
    expect(authorColor("user", COLORS)).toBe("#u1");
    expect(authorColor("system", COLORS)).toBe("#o1");
  });
});