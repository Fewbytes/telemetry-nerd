import { describe, expect, it } from "vitest";
import { facetTop, hitRug, MAX_ROWS, ROW_GAP, ROW_H, RUG_GAP, rugAxisExtra, rugCells, rugHeight, rugHint, rugMoreLabel, rugTop, STATE } from "./rug";
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

  it("an UNKNOWN cell carries the reason it could not be fetched (spec 7.4); other states do not", () => {
    const u = st("a", [STATE.UNKNOWN, STATE.EMPTY], [0, 0]);
    const [c0, c1] = rugCells([u], 60_000, toX);
    expect(rugHint(c0, u, 60_000, "a", true, ["Failed fetches (timeout)."])).toContain("reason: Failed fetches (timeout).");
    expect(rugHint(c1, u, 60_000, "a", true, ["Failed fetches (timeout)."])).not.toContain("reason:");
  });

  it("explains a cell in words", () => {
    const s = st("a", [STATE.EMPTY], [0]);
    const [cell] = rugCells([s], 60_000, toX);
    const text = rugHint(cell, s, 60_000, '{instance="a"}', true);
    expect(text).toContain("no samples");
    expect(text).toContain("0 of 4 expected samples (series reports every 15s)");
    expect(text).toContain('{instance="a"}');
  });

  it("says when the source filled the value", () => {
    const s = { ...st("a", [STATE.PARTIAL], [2]), flags: [16] };
    const [cell] = rugCells([s], 60_000, toX);
    expect(rugHint(cell, s, 60_000, "a", true)).toContain("sample before the gap");
    const u = { ...st("a", [STATE.UNKNOWN], [0]), flags: [8] };
    const [uc] = rugCells([u], 60_000, toX);
    expect(rugHint(uc, u, 60_000, "a", true)).toContain("cannot be observed");
  });

  it("says a cadence-or-loss bucket cannot be told apart", () => {
    const u = { ...st("a", [STATE.UNKNOWN], [0]), flags: [32] };
    const text = rugHint(rugCells([u], 60_000, toX)[0], u, 60_000, "a", true);
    expect(text).toContain("a little slower than the step, or lost a scrape");
  });

  it("names what each function returns after the gap", () => {
    const s = { ...st("a", [STATE.OK], [4]), flags: [16] };
    const text = rugHint(rugCells([s], 60_000, toX)[0], s, 60_000, "a", true);
    expect(text).toContain("increase/delta include the whole gap's change");
    expect(text).toContain("idelta returns the raw sample");
  });

  it("does not claim a post-gap spike on an unknown cell", () => {
    const u = { ...st("a", [STATE.UNKNOWN], [0]), flags: [8 | 16] };
    const text = rugHint(rugCells([u], 60_000, toX)[0], u, 60_000, "a", true);
    expect(text).toContain("cannot be observed");
    expect(text).not.toContain("before the gap");
  });

  it("says when the series' sample rate changed", () => {
    const s = { ...st("a", [STATE.OK], [1]), flags: [2] };
    const [cell] = rugCells([s], 60_000, toX);
    expect(rugHint(cell, s, 60_000, "a", true)).toContain("sample rate changed here");
    const plain = st("a", [STATE.OK], [4]);
    expect(rugHint(rugCells([plain], 60_000, toX)[0], plain, 60_000, "a", true)).not.toContain("sample rate changed");
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

describe("rug placement geometry", () => {
  it("claims no axis space when nothing is drawn", () => {
    expect(rugAxisExtra(0)).toBe(0);
  });
  it("claims rug height plus the gap, growing per row and capped", () => {
    expect(rugAxisExtra(1)).toBe(rugHeight(1) + RUG_GAP);
    expect(rugAxisExtra(3)).toBeGreaterThan(rugAxisExtra(1));
    expect(rugAxisExtra(99)).toBe(rugAxisExtra(MAX_ROWS));
  });
  it("starts the rug a gap below the plot floor", () => {
    expect(rugTop(200)).toBe(200 + RUG_GAP);
    expect(RUG_GAP).toBeGreaterThanOrEqual(2);
  });
});

describe("facetTop", () => {
  it("adds each preceding rug facet's extra height to the fixed stride", () => {
    const stride = 100 + 14 + 30;
    expect(facetTop([false, false, false], 2, 100)).toBe(2 * stride + 4);
    expect(facetTop([true, false, true], 2, 100)).toBe(2 * stride + rugAxisExtra(1) + 4);
    expect(facetTop([true, true], 0, 100)).toBe(4);
  });
});
