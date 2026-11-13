import { expect, test } from "vitest";
import { cloudGrid, cloudLegendText, cloudTip, type FleetCloud } from "./fleetCloud";
import type { FleetData } from "./fleet";

// 4 members alive; at step 2 one goes silent: the column holds 3/4 instead of 4/4
const cloud: FleetCloud = {
  edges: [0, 10, 20], log: false,
  density: [[0.5, 0.5], [0.5, 0.5], [0.5, 0.25], [0.25, 0.5]],
  silent_n: [0, 0, 1, 1],
  silent_members: [{ id: "pod-3", steps: 2 }], silent_more: 2,
};

test("silent members thin a column instead of leaving the denominator", () => {
  const g = cloudGrid(cloud, 10);
  expect(g.cols).toBe(4);
  const sums = [0, 1, 2, 3].map((k) => g.share[k * 2] + g.share[k * 2 + 1]);
  expect(sums).toEqual([1, 1, 0.75, 0.75]);
  expect(g.max).toBe(0.5);
});

test("merging steps into pixel columns averages shares and keeps the largest silent count", () => {
  const g = cloudGrid(cloud, 2);
  expect(Array.from(g.share)).toEqual([0.5, 0.5, 0.375, 0.375]);
  expect(Array.from(g.silent)).toEqual([0, 1]);
});

test("legend names the silent members and says how many more; tip gives share of alive", () => {
  expect(cloudLegendText(cloud, 4)).toContain("silent while alive: pod-3 (2 steps) and 2 more");
  expect(cloudLegendText({ ...cloud, silent_members: [], silent_more: 0 }, 4)).toContain("no member was silent while alive");
  const d = { alive: [4, 4, 4, 4] } as FleetData;
  expect(cloudTip(d, cloud, 2, 1, "12:00")).toBe("12:00 · 10 – 20: 25% of 4 alive members · 1 silent");
});
