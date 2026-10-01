import { describe, expect, it } from "vitest";
import type { SeriesData } from "../lib/api";
import { badgeText, contextStrip, nonZeroOrigin, offeredViews, resolveY, yStats } from "./yview";

const q = (avg: (number | null)[], count: (number | null)[]): SeriesData => ({
  id: "s", labels: {}, ts: avg.map((_, i) => i * 1000), avg, min: avg, max: avg, count,
});
// panel p8: meaningful 0.4–1.3 s, one faded n=13 bucket at 34 s
const P8 = yStats([q([0.4, 1.3, 34, 0.9], [300, 250, 13, 400])], { quantile: true, nMin: 200 });
const view = (mode: string, extra = {}) => ({ mode, label: mode, ...extra }) as never;

describe("yview", () => {
  it("auto leaves the range to uPlot", () => {
    expect(resolveY(null, P8)).toMatchObject({ range: null, log: false, zoomed: false, refused: null });
  });
  it("meaningful-only ranges over n >= n_min and counts the faded point it clips", () => {
    const r = resolveY(view("meaningful"), P8);
    expect(r.range![0]).toBeGreaterThanOrEqual(0); // never below the natural lower bound
    expect(r.range![1]).toBeCloseTo(1.3 + 0.045, 3); // 5% pad
    expect(r.clipped).toEqual({ above: 1, below: 0, maxAbove: 34 });
    expect(r.zoomed).toBe(true);
    expect(badgeText(view("meaningful"), r, "s")).toBe("y zoomed · meaningful · 1 point above view (max 34 s)");
  });
  it("from zero includes zero; data range pads but stays >= 0 for non-negative data", () => {
    expect(resolveY(view("zero"), P8).range![0]).toBe(0);
    const st = yStats([q([5, 6], [500, 500])], { quantile: false, nMin: null });
    expect(resolveY(view("data"), st).range).toEqual([4.95, 6.05]);
  });
  it("band is exact; a band outside the data is refused and falls back to auto", () => {
    expect(resolveY(view("band", { lo: 0.5, hi: 1 }), P8).range).toEqual([0.5, 1]);
    expect(resolveY(view("band", { lo: 50, hi: 60 }), P8)).toMatchObject({ range: null, refused: expect.stringMatching(/no data/) });
  });
  it("log only when every drawn value is positive; suggested above 2 decades", () => {
    expect(resolveY(view("log"), P8)).toMatchObject({ log: true, range: [0.1, 100] });
    const z = yStats([q([0, 5], [1, 1])], { quantile: false, nMin: null });
    expect(resolveY(view("log"), z).refused).toMatch(/> 0/);
    const offers = Object.fromEntries(offeredViews(P8).map((o) => [o.mode, o]));
    expect(offers.log).toMatchObject({ enabled: true, suggest: false }); // 0.4–34 spans 1.9 decades
    const wide = yStats([q([0.01, 20], [500, 500])], { quantile: false, nMin: null });
    expect(offeredViews(wide).find((o) => o.mode === "log")).toMatchObject({ enabled: true, suggest: true });
    expect(offers.meaningful.enabled).toBe(true);
    expect(Object.fromEntries(offeredViews(z).map((o) => [o.mode, o])).log.enabled).toBe(false);
    expect(offeredViews(z).some((o) => o.mode === "meaningful")).toBe(false); // not a quantile
  });
  it("origin marker and context strip", () => {
    expect(nonZeroOrigin(0.3, 1.4, false)).toBe(true);
    expect(nonZeroOrigin(0, 1.4, false)).toBe(false);
    expect(nonZeroOrigin(0.3, 1.4, true)).toBe(false); // log axes have no zero
    expect(contextStrip({ lo: 0, hi: 100 }, [10, 30])).toEqual({ bottomPct: 10, heightPct: 20 });
  });
});
