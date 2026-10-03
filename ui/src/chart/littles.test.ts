import { expect, test } from "vitest";
import { PEAK_COMMON, RATIO_FLOOR, discrepancyText, envelope, flaggedSpans, promotedSpans, ratioRange, littlesLegend, seriesTitle, systematicLabel, toLittlesUplot, toRatioUplot, transientSpans, verdictText, windowTip, type LittlesSeries, type LittlesWindow } from "./littles";

const w = (i: number, over: Partial<LittlesWindow> = {}): LittlesWindow => ({
  start_ms: i * 300_000, end_ms: (i + 1) * 300_000, n: 20, verdict: "consistent",
  L: 2, L_ci: [1.5, 2.5], lambda_W: 2, lambda_W_ci: [1.9, 2.1], ratio: 1, ci95: [0.7, 1.3], flags: [], ...over,
});
const s: LittlesSeries = {
  id: "total", labels: {}, verdict: "inconsistent_in_windows",
  pooled: w(0, { end_ms: 900_000, ratio: 1.2, ci95: [0.95, 1.4] }),
  windows: [w(0), w(1, { verdict: "L_high", L: 5, ratio: 2.5, ci95: [1.8, 3.2], flags: ["not_steady"] }), w(3)],
};

test("steps: one row per window start, a null row at the gap, a closing row at the last end", () => {
  const m = toLittlesUplot(s);
  expect(m.data[0]).toEqual([0, 300, 600, 900, 1200]);
  expect(m.data[3]).toEqual([2, 5, null, 2, 2]);
  expect(m.bands.map((b) => b.series)).toEqual([[2, 1], [5, 4]]);
  const r = toRatioUplot(s);
  expect(r.data[3]).toEqual([1, 2.5, null, 1, 1]);
  expect(r.data[4]).toEqual([1, 1, null, 1, 1]);
  expect(r.bands.map((b) => b.series)).toEqual([[2, 1], [6, 5]]);
});

test("flagged windows, tip and legend", () => {
  expect(flaggedSpans(s)).toEqual([{ x0: 300, x1: 600, verdict: "L_high" }]);
  const tip = windowTip(s, 400)!;
  expect(tip).toContain("L higher than λ·W");
  expect(tip).toContain("L ÷ λW 2.5 [1.8, 3.2] (measurement 95%)");
  expect(tip).toContain("flags: not steady");
  expect(windowTip(s, 700)).toBeNull();
  expect(littlesLegend(s, 300_000)).toContain("L ÷ λW 1.2, +20% (-5% to +40%)");
  expect(littlesLegend(s, 300_000)).toContain("no systematic offset");
  expect(verdictText("inconsistent_in_windows")).toBe("inconsistent in some windows");
  expect(seriesTitle({ ...s, labels: { instance: "i0" } })).toBe("instance=i0");
  expect(seriesTitle(s)).toBe("total");
});

test("an unjudged window reads its reason", () => {
  const u = { ...s, windows: [w(0, { verdict: "no_traffic", ratio: null, reason: "no arrivals" })] };
  expect(windowTip(u, 10)).toContain("no arrivals");
});

test("ratio strip: log-safe floor and a range that always shows ÷2..×2", () => {
  const z = { ...s, windows: [w(0, { ratio: 0, ci95: [0, 0.2] }), w(1, { ratio: 3, ci95: [2, 5] })] };
  const r = toRatioUplot(z);
  expect(r.data[3]).toEqual([RATIO_FLOOR, 3, 3]);
  expect(ratioRange(r.data)).toEqual([RATIO_FLOOR, 5]);
  expect(ratioRange(toRatioUplot({ ...s, windows: [w(0)] }).data)).toEqual([0.5, 2]);
});

const sysSeries: LittlesSeries = {
  id: "total", labels: {}, verdict: "L_high", reference: 2,
  pooled: w(0, { end_ms: 1_200_000, L: 4.4, lambda_W: 2, diff: 2.4, ratio: 2.2, ci95: [2.1, 2.3] }),
  windows: [
    w(0, { ratio: 2, ci95: [1.8, 2.2], diff: 2, diff_ci: [1.6, 2.4], common: 0.1, reference: 2, source: "measurement_system" }),
    w(1, { ratio: 3, ci95: [2.8, 3.2], diff: 4, common: 0.1, reference: 2, source: "special_cause" }),
    w(2, { ratio: 2.15, ci95: [2.1, 2.2], diff: 2.3, common: 0.1, reference: 2, source: "common_cause" }),
    w(3, { ratio: 1.9, ci95: [1.7, 2.1], diff: 1.8, common: 0.1, reference: 2, source: "measurement_system" }),
  ],
  systematic: { direction: "L_high", ratio: 2, ci95: [1.9, 2.1], windows: [3, 4], drifting: false },
  transient: [{ index: 1, phase: "peak", source: "special_cause" }, { index: 2, phase: "other", source: "common_cause" }],
  common_cause: { completions_per_window: 600, rel95: 0.1, spread_rel: 0.2, warning: "at this traffic ..." },
};

test("discrepancy strip: measurement band, common-cause envelope around the systematic level", () => {
  const r = toRatioUplot(sysSeries);
  expect(r.data[7]).toEqual([2, 2, 2, 2, 2]); // the reference: the systematic offset
  expect(envelope(sysSeries, sysSeries.windows[0])).toBe(0.2); // the windows' own spread is wider
  expect(r.data[5][0]).toBeCloseTo(1.6);
  expect(r.data[6][0]).toBeCloseTo(2.4);
  expect(r.data[1]).toEqual([1.8, 2.8, 2.1, 1.7, 1.7]);
});

test("transients are marked with their source; the systematic offset is labelled", () => {
  expect(transientSpans(sysSeries)).toEqual([
    { x0: 300, x1: 600, source: "special_cause", phase: "peak" },
    { x0: 600, x1: 900, source: "common_cause", phase: "other" },
  ]);
  expect(systematicLabel(sysSeries)).toBe("systematic offset L ÷ λW 2 [1.9, 2.1] in 3/4 windows (measurement system)");
  expect(systematicLabel(s)).toBeNull();
  const tip = windowTip(sysSeries, 400)!;
  expect(tip).toContain("L − λW +4");
  expect(tip).toContain("TRANSIENT (special cause, at a load peak)");
  expect(tip).toContain("common cause ±20% around 2");
  expect(windowTip(sysSeries, 10)).toContain("L − λW +2 [1.6, 2.4]");
  expect(windowTip(sysSeries, 10)).toContain("source: measurement system");
  expect(discrepancyText(sysSeries)).toBe("L − λW +2.4 · L ÷ λW 2.2, +120% (+110% to +130%)");
  const legend = littlesLegend(sysSeries, 300_000);
  expect(legend.startsWith("Whole range L − λW +2.4")).toBe(true);
  expect(legend).toContain("2 transient windows (1 special cause) marked");
  expect(legend).toContain("±10% at N≈600/window");
  expect(flaggedSpans(sysSeries)).toEqual([]);
});

test("promoted load-peak windows: marked, the hover says why; a common-cause peak is not a signal by itself", () => {
  const reason = "backlog grew +376 requests in the window (gauge +376, arrivals − completions +377): 155× the steady-state scale √2·σ_N = 2.42 (threshold 26.8; Cantelli p ≤ 4.1e-05)";
  const p: LittlesSeries = {
    ...s, verdict: "consistent",
    windows: [
      w(0, { source: "measurement_system", common: 0.17 }),
      w(1, { ratio: 1.0, source: "special_cause", common: 0.17 }),
      w(2, { ratio: 1.12, source: "common_cause", common: 0.17 }),
    ],
    transient: [{ index: 2, phase: "peak", source: "common_cause", promoted: false }],
    promoted: [{ index: 1, from: "measurement_system", deviation: "within_measurement", reason, evidence: ["backlog_growth"] }],
  };
  expect(promotedSpans(p)).toEqual([{ x0: 300, x1: 600, from: "measurement_system", reason }]);
  expect(promotedSpans(s)).toEqual([]);
  const tip = windowTip(p, 400)!;
  expect(tip).toContain("SPECIAL CAUSE — promoted from measurement system: at a load peak, leaving steady state");
  expect(tip).toContain(`why: ${reason}`);
  expect(windowTip(p, 700)).toContain(`TRANSIENT (common cause): ${PEAK_COMMON}`);
  expect(littlesLegend(p, 300_000)).toContain("1 load-peak window promoted to special cause on evidence of leaving steady state");
  expect(littlesLegend(s, 300_000)).not.toContain("promoted");
});

test("windows found on the grid shifted by half a window are marked with their own span", () => {
  const p: LittlesSeries = {
    ...sysSeries,
    transient: [{ index: null, grid: "offset", start_ms: 450_000, end_ms: 750_000, phase: "drain", source: "special_cause" }],
    promoted: [{ index: null, grid: "offset", start_ms: 150_000, end_ms: 450_000, from: "common_cause", deviation: "within_envelope", reason: "r", evidence: ["backlog_growth"] }],
  };
  expect(transientSpans(p)).toEqual([{ x0: 450, x1: 750, source: "special_cause", phase: "drain" }]);
  expect(promotedSpans(p)).toEqual([{ x0: 150, x1: 450, from: "common_cause", reason: "r" }]);
});
