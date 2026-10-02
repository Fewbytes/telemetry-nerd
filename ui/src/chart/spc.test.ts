import { expect, test } from "vitest";
import {
  baselineSpan, limitIntervals, markTip, nearestMark, spcLegend, toSpcUplot, violationMarks, type SpcSeries,
} from "./spc";

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

const withIv: SpcSeries = { ...s, level: 2, centre_interval: [1.8, 2.3], sigma_interval: [0.9, 1.2] };

test("limit uncertainty: each limit inherits the level's and sigma's intervals", () => {
  expect(limitIntervals(s)).toBeNull();
  const iv = limitIntervals(withIv)!;
  expect(iv.dlo).toBeCloseTo(-0.2);
  expect(iv.dhi).toBeCloseTo(0.3);
  const m = toSpcUplot(withIv, 60_000);
  const col = (k: number) => (m.data[k] as (number | null)[])[0]!;
  expect(col(5)).toBeCloseTo(2 - 0.2 + 2.7); // upper limit, low end
  expect(col(6)).toBeCloseTo(2 + 0.3 + 3.6); // upper limit, high end
  expect(col(7)).toBeCloseTo(2 - 0.2 - 3.6); // lower limit, low end
  expect(col(8)).toBeCloseTo(2 + 0.3 - 2.7);
  expect([col(9), col(10)]).toEqual([1.8, 2.3]);
  expect(m.bands.map((b) => b.series)).toEqual([[4, 3], [6, 5], [8, 7], [10, 9]]);
  expect((m.data[5] as (number | null)[])[3]).toBeNull(); // the gap row breaks the strips too
  expect(toSpcUplot(s, 60_000).bands).toHaveLength(1); // no intervals: just the 3σ band
});

test("supplementary run rules can be hidden; hover finds the nearest mark and names its rules", () => {
  expect(violationMarks(s, false).map((m) => m.x)).toEqual([300]);
  const marks = violationMarks(s);
  const px = (m: { x: number; y: number }): [number, number] => [m.x, 100 - m.y * 10];
  expect(nearestMark(marks, 302, 12, px)?.x).toBe(300);
  expect(nearestMark(marks, 250, 12, px)).toBeNull();
  const tip = markTip(marks[0], 60_000);
  expect(tip.split("\n")[0]).toBe("00:04–00:05 UTC · 9");
  expect(tip).toContain("outside centre ± 3σ");
  expect(tip).toContain("CUSUM (k 0.5, h 5)");
  expect(markTip(marks[1], 60_000)).toContain("8 points in a row on one side of the centre (supplementary)");
});

test("legend names a separately fetched baseline, the profile's seasonal centre and the strips", () => {
  const ref = { start_ms: 0, end_ms: 3_600_000, basis: "the same 1h window on the previous week, aligned by UTC (fetched separately)", kind: "reference" as const };
  const prof = { ...withIv, seasonal: "profile", seasonal_profile: { profile: "op-1", expr: "x", model: "hour_of_day", history: "30d", judged_hours_excluded: 2 } };
  const l = spcLegend(prof, ref, false);
  expect(l).toContain("previous week");
  expect(l).toContain("not drawn; every point shown is judged");
  expect(l).not.toContain("shaded");
  expect(l).toContain("operating profile's daily shape (30d, judged hours left out)");
  expect(l).toContain("99% intervals");
  expect(l).toContain("supplementary run rules hidden");
});
