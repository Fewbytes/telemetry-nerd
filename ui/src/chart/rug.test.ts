import { describe, expect, it } from "vitest";
import { hitRug, MAX_ROWS, ROW_GAP, ROW_H, rugCells, rugHeight, rugHint, STATE } from "./rug";
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
    const text = rugHint(cell, s, 60_000, '{instance="a"}', 15_000);
    expect(text).toContain("no samples");
    expect(text).toContain("0 of 4 expected");
    expect(text).toContain('{instance="a"}');
  });
});
