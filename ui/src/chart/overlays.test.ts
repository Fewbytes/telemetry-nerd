import { describe, expect, it } from "vitest";
import type { OverlaysPayload, SeriesData } from "../lib/api";
import { overlayChips, overlayDraw } from "./overlays";
import { LIMIT_COLOR, toUplot } from "./toUplot";

const ov = (o: Partial<OverlaysPayload> = {}): OverlaysPayload => ({
  flags: { normal: true, limit: true, ghost: false },
  normal: { available: true, label: "normal range (30d, same hour of week)", series: { a: { ts: [1000, 2000], lo: [1, 2], hi: [5, 6] } }, unmatched: [] },
  limit: { available: true, label: "limit size_bytes", metric: "size_bytes", hi: 9, series: [{ id: "l", labels: { __name__: "size_bytes" }, ts: [1000, 2000], avg: [9, 9], min: [9, 9], max: [9, 9], count: [1, 1] }] },
  ghost: { available: true, loaded: false, label: "last week" },
  ...o,
});
const s = (id: string, ts: number[], v: number[]): SeriesData => ({ id, labels: { i: id }, ts, avg: v, min: v, max: v, count: v.map(() => 4) });

describe("overlayChips", () => {
  it("lists the three layers with their state", () => {
    const chips = overlayChips(ov());
    expect(chips.map((c) => [c.key, c.on, c.enabled])).toEqual([["normal", true, true], ["limit", true, true], ["ghost", false, true]]);
    expect(chips[0].title).toMatch(/operating profile/);
    expect(chips[1].title).toMatch(/bounded_by/);
  });
  it("an unavailable layer is disabled and says why", () => {
    const chips = overlayChips(ov({ normal: { available: false, reason: "the operating profile is not computed yet" }, limit: { available: false, reason: "the catalog has no bounded_by relation for this metric" } }));
    expect(chips[0]).toMatchObject({ on: false, enabled: false });
    expect(chips[0].title).toMatch(/not computed yet/);
    expect(chips[1].title).toMatch(/no bounded_by relation/);
  });
  it("a flag that is on but unavailable does not look on", () => {
    expect(overlayChips(ov({ normal: { available: false, reason: "x" } }))[0].on).toBe(false);
  });
  it("mentions stale profiles and series without a profile", () => {
    const t = overlayChips(ov({ normal: { available: true, label: "n", stale: true, series: {}, unmatched: ["x", "y"] } }))[0].title;
    expect(t).toMatch(/stale/);
    expect(t).toMatch(/2 series have no profile/);
  });
  it("ghost: fetched-on-switch-on until loaded", () => {
    expect(overlayChips(ov())[2].title).toMatch(/fetched when switched on/);
    expect(overlayChips(ov({ ghost: { available: true, loaded: true } }))[2].title).toMatch(/one week earlier, dashed/);
  });
});

describe("overlayDraw", () => {
  it("draws only layers that are on, available and carry data", () => {
    const d = overlayDraw(ov())!;
    expect(Object.keys(d)).toEqual(["normal", "limit"]);
    expect(overlayDraw(ov({ flags: { normal: false, limit: false, ghost: false } }))).toBeUndefined();
    const g = overlayDraw(ov({ flags: { normal: false, limit: false, ghost: true }, ghost: { available: true, loaded: true, series: [{ id: "a", ts: [1000], avg: [1], count: [4] }] } }))!;
    expect(Object.keys(g)).toEqual(["ghost"]);
    expect(overlayDraw(ov({ flags: { normal: false, limit: false, ghost: true } }))).toBeUndefined(); // on but not fetched yet
    expect(overlayDraw(ov({ flags: { normal: false, limit: false, ghost: true }, ghost: { available: true, loaded: true, series: [] } }))).toBeUndefined(); // fetched, nothing there
    expect(overlayChips(ov({ ghost: { available: true, loaded: true, series: [] } }))[2].title).toMatch(/no data in the same window one week earlier/);
    expect(overlayDraw(null)).toBeUndefined();
  });
});

describe("toUplot overlays", () => {
  const main = [s("a", [1000, 2000], [3, 4])];

  it("puts the band under its series, hidden from the legend, filling between lo and hi", () => {
    const m = toUplot(main, undefined, { overlays: overlayDraw(ov()) });
    const li = m.series.findIndex((x) => String(x.label).includes("normal low"));
    const mainIdx = m.series.findIndex((x) => x.label === 'i="a"' || (String(x.label).includes('i="a"') && !String(x.label).includes("normal")));
    expect(li).toBeGreaterThan(0);
    expect(li).toBeLessThan(mainIdx); // drawn first, underneath
    expect(m.legendHidden).toContain(li);
    expect(m.legendHidden).toContain(li + 1);
    expect(m.data[li]).toEqual([1, 2]);
    expect(m.data[li + 1]).toEqual([5, 6]);
    expect(m.bands.some((b) => b.series[0] === li + 1 && b.series[1] === li)).toBe(true);
  });
  it("draws the limit as a dashed hazard-coloured line", () => {
    const m = toUplot(main, undefined, { overlays: overlayDraw(ov()) });
    const i = m.series.findIndex((x) => String(x.label).startsWith("limit "));
    expect(m.series[i]).toMatchObject({ stroke: LIMIT_COLOR });
    expect(m.series[i].dash).toBeTruthy();
    expect(m.data[i]).toEqual([9, 9]);
    expect(m.legendHidden).toContain(i);
  });
  it("draws the ghost faintly and dashed, matched by series id; unmatched ghosts are ignored", () => {
    const d = overlayDraw(ov({ flags: { normal: false, limit: false, ghost: true }, ghost: { available: true, loaded: true, series: [{ id: "a", ts: [1000, 2000], avg: [7, 8], count: [4, 4] }, { id: "zzz", ts: [1000], avg: [1], count: [4] }] } }));
    const m = toUplot(main, undefined, { overlays: d });
    const gi = m.series.findIndex((x) => String(x.label).endsWith("last week"));
    expect(gi).toBeGreaterThan(0);
    expect(m.data[gi]).toEqual([7, 8]);
    expect(m.series.filter((x) => String(x.label).endsWith("last week"))).toHaveLength(1);
  });
  it("a percentile ghost keeps the n gate: low-count buckets are not drawn", () => {
    const d = overlayDraw(ov({ flags: { normal: false, limit: false, ghost: true }, ghost: { available: true, loaded: true, series: [{ id: "a", ts: [1000, 2000], avg: [7, 8], count: [500, 3] }] } }));
    const m = toUplot(main, undefined, { quantile: true, nMin: 200, overlays: d });
    const gi = m.series.findIndex((x) => String(x.label).endsWith("last week"));
    expect(m.data[gi]).toEqual([7, null]);
  });
  it("overlay timestamps join the time axis", () => {
    const d = overlayDraw(ov({ normal: { available: true, series: { a: { ts: [3000], lo: [1], hi: [2] } } } }));
    expect(toUplot(main, undefined, { overlays: d }).data[0]).toEqual([1, 2, 3]);
  });
  it("without overlays the model is unchanged", () => {
    const plain = toUplot(main);
    const again = toUplot(main, undefined, { overlays: undefined });
    expect(again.data).toEqual(plain.data); // (series hold per-call closures, so compare the content)
    expect(again.bands).toEqual(plain.bands);
    expect(again.legendHidden).toEqual(plain.legendHidden);
    expect(again.series.map((x) => x.label)).toEqual(plain.series.map((x) => x.label));
    expect(plain.series).toHaveLength(4); // x + avg/min/max
  });
});
