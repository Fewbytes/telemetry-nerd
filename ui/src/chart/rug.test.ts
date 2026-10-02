import { describe, expect, it } from "vitest";
import { hitRug, MAX_ROWS, ROW_GAP, ROW_H, rugCells, rugHeight, rugHint, rugMoreLabel, STATE } from "./rug";
import { relativeLuminance } from "./colormap";
import type { BucketStatePayload } from "../lib/api";

const st = (id: string, state: number[], observed: number[]): BucketStatePayload => ({
  id, ts: state.map((_, i) => (i + 1) * 60_000), state, observed, expected: state.map(() => 4), flags: state.map(() => 0),
});

describe("rug", () => {
  const toX = (ms: number) => ms / 1000; // 1 px per second

  it("lays out one cell per bucket per row, across the bucket interval", () => {
    const cells = rugCells([st("a", [STATE.OK, STATE.PARTIAL], [4, 1])], 60_000, toX);
    expect(cells).toHaveLength(2);
    expect(cells[1]).toMatchObject({ row: 0, x: 60, w: 60, state: STATE.PARTIAL, missing: 0.75 });
  });

  it("caps rows and sizes the canvas", () => {
    const many = Array.from({ length: 7 }, (_, k) => st(`s${k}`, [STATE.EMPTY], [0]));
    expect(new Set(rugCells(many, 60_000, toX).map((c) => c.row)).size).toBe(MAX_ROWS);
    expect(rugHeight(0)).toBe(0);
    expect(rugHeight(2)).toBe(2 * (ROW_H + ROW_GAP) + 2);
  });

  it("hit-tests by row and x", () => {
    const cells = rugCells([st("a", [STATE.OK], [4]), st("b", [STATE.EMPTY], [0])], 60_000, toX);
    expect(hitRug(cells, 10, ROW_H + ROW_GAP + 2)?.row).toBe(1);
    expect(hitRug(cells, 10, 500)).toBeNull();
  });

  it("explains a cell in words", () => {
    const s = st("a", [STATE.EMPTY], [0]);
    const [cell] = rugCells([s], 60_000, toX);
    const text = rugHint(cell, s, 60_000, '{instance="a"}', true);
    expect(text).toContain("no samples");
    expect(text).toContain("0 of 4 expected samples (series reports every 15s)");
    expect(text).toContain('{instance="a"}');
  });

  it("derives the interval from the series' own expected count, not a preset", () => {
    const s = { ...st("a", [STATE.PARTIAL], [0]), expected: [1] };
    const [cell] = rugCells([s], 60_000, toX);
    expect(rugHint(cell, s, 60_000, "a", true)).toContain("0 of 1 expected samples (series reports every 1m)");
  });

  it("has no samples line in presence mode (quantiles, heatmap columns)", () => {
    const s = st("a", [STATE.EMPTY], [0]);
    const [cell] = rugCells([s], 60_000, toX);
    expect(rugHint(cell, s, 60_000, "a", false)).not.toContain("expected samples");
  });
});

describe("rug grey", () => {
  const ratio = (a: string, b: string) => {
    const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  // --muted / --bg values from ui/src/index.css
  it("--muted passes 3:1 in both themes", () => {
    expect(ratio("#666666", "#ffffff")).toBeGreaterThanOrEqual(3);
    expect(ratio("#9aa0a6", "#16181d")).toBeGreaterThanOrEqual(3);
  });
});

describe("rugMoreLabel", () => {
  it("says nothing when no series were left out", () => {
    expect(rugMoreLabel(0)).toBe("");
  });
  it("counts the omitted series and points to the footer", () => {
    expect(rugMoreLabel(3)).toBe("+3 more series with coverage issues (see footer)");
  });
});
