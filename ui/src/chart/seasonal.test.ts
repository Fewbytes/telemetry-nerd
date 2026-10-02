import { expect, test } from "vitest";
import { flagMarks, seasonalLegend, seriesDisplayName, toSeasonalUplot, verdictText, type SeasonalSeries } from "./seasonal";

const s: SeasonalSeries = {
  id: "a", labels: { job: "api" }, ts: [0, 300_000, 600_000], now: [10, null, 30],
  verdict: "unusual", direction: "higher", reasons: ["window level x1.35"],
  scheme: "1w", label: "median of the same window on the previous 2 weeks, aligned by UTC", scale: "log",
  cycles: [{ j: 1, start_ms: -604_800_000, values: [9, 10, 11] }, { j: 2, start_ms: -1_209_600_000, values: [8, null, 12] }],
  excluded: [{ j: 3, start_ms: -1_814_400_000, reason: "user" }], n: 2,
  centre: [8.5, 10, 11.5], lo: [7, 8, 9], hi: [11, 12, 14],
  ratio: { kind: "ratio", value: [1.18, null, 2.6], lo: [0.8, 0.8, 0.8], hi: [1.25, 1.25, 1.25] },
  flagged: [{ ts: 600_000, value: 30, z: 6.1 }], n_eff: 2,
};

test("overlay: faint cycles, band, centre, then now; gaps stay null", () => {
  const m = toSeasonalUplot(s, "overlay");
  expect(m.roles).toEqual(["x", "cycle", "cycle", "lo", "hi", "centre", "now"]);
  expect(m.data[0]).toEqual([0, 300, 600]);
  expect(m.data[2]).toEqual([8, null, 12]);
  expect(m.data[6]).toEqual([10, null, 30]);
  expect(m.bands[0].series).toEqual([4, 3]);
});

test("ratio view: band and a neutral line at 1 (0 for differences)", () => {
  const m = toSeasonalUplot(s, "ratio");
  expect(m.roles).toEqual(["x", "lo", "hi", "neutral", "now"]);
  expect(m.data[3]).toEqual([1, 1, 1]);
  const d = toSeasonalUplot({ ...s, ratio: { ...s.ratio!, kind: "difference" } }, "ratio");
  expect(d.data[3]).toEqual([0, 0, 0]);
});

test("legend states reference, band method and exclusions; flags follow the view", () => {
  expect(seasonalLegend(s, "UTC", "overlay")).toContain("previous 2 weeks, aligned by UTC");
  expect(seasonalLegend(s, "UTC", "overlay")).toContain("excluded: −3 (user)");
  expect(seasonalLegend(s, "UTC", "ratio")).toContain("now ÷ reference");
  expect(seasonalLegend({ ...s, verdict: "insufficient_history", reasons: ["2 usable previous 1w cycles (< 3)"] }, "UTC", "overlay"))
    .toBe("Not enough history: 2 usable previous 1w cycles (< 3).");
  expect(flagMarks(s, "overlay")).toEqual([{ x: 600, y: 30, text: "z +6.1" }]);
  expect(flagMarks(s, "ratio")[0].y).toBe(2.6);
  expect(verdictText(s)).toBe("unusual (higher)");
});

test("seriesDisplayName: labelled series keep their name; a fully unlabelled series (sum without()) falls back to the expr", () => {
  expect(seriesDisplayName(s, "sum without()(up)")).toBe('{job="api"}');
  expect(seriesDisplayName({ ...s, labels: {} }, "sum without()(up)")).toBe("sum without()(up)");
});
