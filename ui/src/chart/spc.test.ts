import { expect, test } from "vitest";
import { baselineSpan, spcLegend, toSpcUplot, violationMarks, type SpcSeries } from "./spc";

const s: SpcSeries = {
  id: "a", labels: { job: "x" }, ts: [0, 60_000, 120_000, 300_000], value: [1, 2, 3, 9],
  verdict: "level_shifted", also: [], n: 4, mode: "individuals",
  centre: [2, 2, 2, 2], sigma: 1, n_baseline: 2, n_eff_baseline: 2, in_control: false,
  violations: [
    { ts: 300_000, value: 9, rules: ["outside_limits", "cusum"] },
    { ts: 120_000, value: 3, rules: ["8_in_a_row_one_side"] },
  ],
};
const b = { start_ms: 0, end_ms: 120_000, basis: "first half of the range (default)" };

test("columns: value, centre and 3-sigma band; a null row breaks the line at a gap", () => {
  const m = toSpcUplot(s, 60_000);
  expect(m.data[0]).toEqual([0, 60, 120, 180, 300]);
  expect(m.data[1]).toEqual([1, 2, 3, null, 9]);
  expect(m.data[3]).toEqual([-1, -1, -1, null, -1]);
  expect(m.data[4]).toEqual([5, 5, 5, null, 5]);
  expect(m.bands[0].series).toEqual([4, 3]);
});

test("baseline is clipped to the plot; violations split into deciding and supplementary", () => {
  expect(baselineSpan(b, 30, 300)).toEqual([30, 120]);
  expect(baselineSpan(b, 200, 300)).toBeNull();
  const v = violationMarks(s);
  expect(v.map((m) => m.deciding)).toEqual([true, false]);
  expect(v[0].text).toBe("outside the 3σ band, CUSUM");
});

test("legend states the baseline, mode and verdict on control", () => {
  expect(spcLegend(s, b)).toContain("first half (default)");
  expect(spcLegend(s, b)).toContain("out of control: 1 point");
  expect(spcLegend({ ...s, mode: "ar1_residuals" }, b)).toContain("AR(1) residuals");
  expect(spcLegend({ ...s, mode: "insufficient_data", reason: "baseline has 3 points" }, b)).toBe(
    "No control limits: baseline has 3 points.",
  );
});
