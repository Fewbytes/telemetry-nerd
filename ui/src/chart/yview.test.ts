import { describe, expect, it } from "vitest";
import type { SeriesData, YContext, YView } from "../lib/api";
import { badgeText, contextStrip, nonZeroOrigin, offeredViews, refExtent, resolveY, yStats } from "./yview";

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
  it("indexed: log, symmetric around 1, badge says what 1 means", () => {
    const st = yStats([q([0.8, 1.4], [500, 500])], { quantile: false, nMin: null });
    const r = resolveY(view("indexed", { baseline: "window" }), st);
    expect(r).toMatchObject({ log: true, zoomed: false, refused: null });
    expect(r.range![1]).toBe(1.5);
    expect(badgeText(view("indexed", { baseline: "window" }), r, null, "1 = each series' mean over 12:00–15:00Z"))
      .toBe("indexed · 1 = each series' mean over 12:00–15:00Z");
  });
});

describe("yview with catalog context (2as.10)", () => {
  const ctx = (o: Partial<YContext> = {}): YContext => ({
    natural_lo: null, natural_hi: null, bounds: null, bounds_origin: null, limit: null, profile: null, notes: [], ...o,
  });
  const stats = (vals: number[]) => yStats([{ id: "s", labels: {}, ts: vals.map((_, i) => i), avg: vals, min: vals, max: vals, count: vals.map(() => 1) }], { quantile: false, nMin: null });
  const view = (mode: YView["mode"], label: string = mode): YView => ({ mode, label });

  it("reference unions data, normal range and physical limit", () => {
    const st = stats([40, 50]);
    const r = resolveY(view("reference"), st, ctx({ profile: { lo: 10, hi: 90, label: "normal" }, limit: { metric: "size", dataset: "d2", hi: 200, basis: "bounded_by" } }));
    expect(r.range![0]).toBeLessThanOrEqual(10);
    expect(r.range![1]).toBeGreaterThanOrEqual(200);
    expect(r.zoomed).toBe(false);
    expect(r.reference).toBe(true);
  });
  it("small wiggles on a big signal look small under the reference range", () => {
    const st = stats([50, 51]);
    const c = ctx({ profile: { lo: 0, hi: 100, label: "normal" } });
    const [lo, hi] = resolveY(view("reference"), st, c).range!;
    expect((51 - 50) / (hi - lo)).toBeLessThan(0.02);
    const [dlo, dhi] = resolveY(view("data"), st, c).range!;
    expect((51 - 50) / (dhi - dlo)).toBeGreaterThan(0.5);
  });
  it("data mode is zoomed and its strip is measured against the reference extent", () => {
    const st = stats([50, 51]);
    const c = ctx({ profile: { lo: 0, hi: 100, label: "normal" } });
    const r = resolveY(view("data"), st, c);
    expect(r.zoomed).toBe(true);
    expect(r.spanPct).toBeLessThan(15); // % of the reference range, not of the data
    const cs = contextStrip(refExtent(st.all!, c), r.range!);
    expect(cs.heightPct).toBeLessThan(15);
  });
  it("a reference without inputs is just a data fit and says so", () => {
    const r = resolveY(view("reference"), stats([5, 6]), ctx());
    expect(r.zoomed).toBe(true);
    expect(r.reference).toBe(false);
  });
  it("never extends below a natural lower bound, nor above an upper one", () => {
    const [lo] = resolveY(view("data"), stats([0.01, 0.5]), ctx({ natural_lo: 0, bounds: "≥0" })).range!;
    expect(lo).toBe(0);
    const [, hi] = resolveY(view("data"), stats([0.2, 0.99]), ctx({ natural_lo: 0, natural_hi: 1, bounds: "[0,1]" })).range!;
    expect(hi).toBe(1);
  });
  it("the catalog's unbounded answer lifts the data>=0 assumption", () => {
    const [lo] = resolveY(view("data"), stats([2, 3]), ctx({ bounds: "none" })).range!;
    expect(lo).toBeLessThan(2);
    const [lo0] = resolveY(view("data"), stats([2, 3]), null).range!;
    expect(lo0).toBeGreaterThanOrEqual(0); // no catalog: legacy assumption
  });
  it("semantic is the natural bounds; open sides follow the data", () => {
    expect(resolveY(view("semantic"), stats([0.2, 0.4]), ctx({ natural_lo: 0, natural_hi: 1, bounds: "[0,1]" })).range).toEqual([0, 1]);
    const [lo, hi] = resolveY(view("semantic"), stats([3, 8]), ctx({ natural_lo: 0, bounds: "≥0" })).range!;
    expect(lo).toBe(0);
    expect(hi).toBeGreaterThan(8);
  });
  it("semantic without bounds is refused with a reason", () => {
    const r = resolveY(view("semantic", "natural bounds"), stats([1, 2]), ctx());
    expect(r.refused).toMatch(/no natural bounds/);
    expect(r.range).toBeNull();
  });
  it("auto resolves to reference when a reference exists, else stays uPlot's own", () => {
    const c = ctx({ limit: { metric: "size", dataset: "d2", hi: 10, basis: "bounded_by" } });
    expect(resolveY(null, stats([1, 2]), c).effective?.mode).toBe("reference");
    expect(resolveY(null, stats([1, 2]), ctx()).range).toBeNull();
    expect(resolveY(view("auto"), stats([1, 2]), null).effective).toBeNull();
  });
  it("auto goes log when positive data spans more than two decades, and labels it", () => {
    const r = resolveY(null, stats([1, 5, 5000]), null);
    expect(r.log).toBe(true);
    expect(r.effective?.label).toMatch(/auto: 3\.7 decades/);
    expect(resolveY(null, stats([1, 5, 50]), null).log).toBe(false); // < 2 decades
    expect(resolveY(null, stats([0, 5, 5000]), null).log).toBe(false); // log needs every value > 0
    expect(resolveY(null, stats([1, 100]), null).log).toBe(false); // exactly 2 decades is not more
  });
  it("offers semantic only when bounds are known, and explains the reference", () => {
    const st = stats([1, 2]);
    const off = (c: YContext | null) => Object.fromEntries(offeredViews(st, c).map((o) => [o.mode, o]));
    expect(off(ctx()).semantic.enabled).toBe(false);
    expect(off(ctx({ natural_lo: 0, bounds: "≥0" })).semantic.enabled).toBe(true);
    expect(off(ctx({ profile: { lo: 0, hi: 5, label: "n" } })).reference.title).toMatch(/normal range/);
  });
  it("badge says what the reference includes", () => {
    const c = ctx({ profile: { lo: 10, hi: 90, label: "normal range (30d)" }, limit: { metric: "node_filesystem_size_bytes", dataset: "d2", hi: 200, basis: "bounded_by" } });
    const r = resolveY(null, stats([40, 50]), c);
    const text = badgeText(r.effective!, r, null, undefined, c);
    expect(text).toMatch(/reference range/);
    expect(text).toMatch(/normal range \(30d\) 10–90/);
    expect(text).toMatch(/limit node_filesystem_size_bytes 200/);
  });
});
