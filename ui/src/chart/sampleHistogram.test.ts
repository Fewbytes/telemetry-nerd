import { expect, test } from "vitest";
import { layoutSampleBars, sampleBarTip } from "./sampleHistogram";

test("bars span the shared edges and the tallest count fills the height", () => {
  const bars = layoutSampleBars([0, 10, 20], [5, 10], 10, 200, 100);
  expect(bars.map((b) => [b.x, b.height, b.y])).toEqual([[0, 50, 50], [100, 100, 0]]);
});

test("two columns with the same count draw bars of the same height", () => {
  const a = layoutSampleBars([0, 1, 2], [3, 0], 3, 100, 60);
  const b = layoutSampleBars([0, 1, 2], [3, 1], 3, 100, 60);
  expect(a[0].height).toBe(b[0].height);
  expect(a[1].height).toBe(0);
});

test("a single-value column (zero span) still draws a bar", () => {
  const [bar] = layoutSampleBars([5, 5], [4], 4, 100, 40);
  expect(bar.width).toBeGreaterThan(0);
  expect(bar.height).toBe(40);
});

test("the tooltip names the bin and its count", () => {
  expect(sampleBarTip({ x: 0, y: 0, width: 1, height: 1, lo: 1, hi: 2, count: 7 })).toBe("1 to 2: 7");
});
