import { expect, test } from "vitest";
import { relativeLuminance } from "./colormap";
import { BAND_ALPHAS, FLEET_HUE, bandFills, fleetAxisLabel, fleetKey, nearestOutlier, outlierMarkIdx, fleetY, coverageGaps, fleetLegend, outlierText, toFleetUplot, type FleetData } from "./fleet";

const d: FleetData = {
  ts: [0, 300_000, 600_000], members: 100, normalise: "none", scale: "log",
  band: {
    median: [10, 11, null], q25: [9, 10, null], q75: [11, 12, null], q10: [8, 9, null],
    q90: [12, 13, null], lo: [5, 6, 7], hi: [20, 21, 22],
  },
  n: [100, 97, 2], alive: [100, 100, 100], outlier_count: 8,
  outliers: [
    { id: "pod=a", labels: { pod: "a" }, kind: "persistent", direction: "higher", score: 3, values: [18, 19, 20], since_ms: 0, episodes: [] },
    { id: "pod=b", labels: { pod: "b" }, kind: "transient", direction: "lower", score: 2, values: [10, null, 1], since_ms: null, episodes: [[600_000, 600_000]] },
  ],
};

test("columns: nested band fills, median, then drawn outliers; gaps stay null", () => {
  const m = toFleetUplot(d);
  expect(m.roles).toEqual(["x", "lo", "hi", "q10", "q90", "q25", "q75", "median", "outlier", "outlier"]);
  expect(m.data[0]).toEqual([0, 300, 600]);
  expect(m.data[7]).toEqual([10, 11, null]);
  expect(m.data[9]).toEqual([10, null, 1]);
  expect(m.bands.map((b) => b.series)).toEqual([[2, 1], [4, 3], [6, 5]]);
});

test("legend states group size, outliers drawn of found, n per step and the band", () => {
  const s = fleetLegend(d);
  expect(s).toContain("100 members · 8 outliers (2 drawn)");
  expect(s).toContain("2–100 reporting per step");
  expect(s).toContain("min–max, 10–90%, 25–75%");
  expect(fleetLegend({ ...d, outliers: [], outlier_count: 0, normalise: "member" })).toContain("no outliers");
  expect(fleetLegend({ ...d, normalise: "member" })).toContain("relative to its own median");
});

test("outlier text says kind, direction and since; coverage gaps are shares of alive", () => {
  const t = (ms: number) => `t${ms / 1000}`;
  expect(outlierText(d.outliers[0], t)).toBe("pod=a: consistently higher since t0");
  expect(outlierText(d.outliers[1], t)).toBe("pod=b: briefly lower · episode from t600");
  const gaps = coverageGaps(d);
  expect(gaps.map((g) => g.x)).toEqual([300, 600]);
  expect(gaps[0].share).toBeCloseTo(0.03);
  expect(gaps[1].share).toBeCloseTo(0.98);
});

test("a fleet of a bounded metric defaults to its natural bounds, with provenance in the badge (hnt)", async () => {
  const { badgeText } = await import("./yview");
  const u: FleetData = { ...d, scale: "linear", band: { ...d.band, lo: [0.0015, 0.002, 0.002], hi: [0.004, 0.003, 0.004] }, outliers: [] };
  const ctx = { natural_lo: 0, natural_hi: 1, bounds: "[0,1]", bounds_origin: "rule", bounds_basis: "1 − (rate of node_cpu_seconds_total is a fraction of time per series)", bounds_confidence: 0.7, limit: null, profile: null, notes: [] };
  const r = fleetY(u, ctx);
  expect(r.range).toEqual([0, 1]);
  expect(badgeText(r.effective!, r, null, undefined, ctx)).toMatch(/bounds: rule \(confidence 0\.70\)/);
  expect(fleetY(u, null).range).toBeNull(); // unbounded: unchanged
  expect(fleetY({ ...u, normalise: "member" }, ctx).range).toBeNull(); // x own median: not in metric units
  expect(fleetY(u, ctx, { mode: "data", label: "data range" } as never).zoomed).toBe(true);
});

test("the real payload shape (scale log = analysis scale, normalise none) still gets the bounded range", async () => {
  const { badgeText } = await import("./yview");
  const u: FleetData = { ...d, scale: "log", normalise: "none", band: { ...d.band, lo: [0.0015, 0.002, 0.002], hi: [0.004, 0.003, 0.004] }, outliers: [] };
  const ctx = { natural_lo: 0, natural_hi: 1, bounds: "[0,1]", bounds_origin: "rule", bounds_basis: "1 − idle", bounds_confidence: 0.7, limit: null, profile: null, notes: [] };
  const r = fleetY(u, ctx);
  expect(r.range).toEqual([0, 1]);
  expect(badgeText(r.effective!, r, null, undefined, ctx)).toMatch(/natural bounds \[0,1\].*bounds: rule/);
});

test("fleet panels never resolve a log y view: unbounded wide-range data stays on uPlot's range", () => {
  const w: FleetData = { ...d, scale: "log", band: { ...d.band, lo: [0.001, 0.002, 0.003], hi: [5, 4, 3] }, outliers: [] };
  const auto = fleetY(w, null);
  expect(auto.log).toBe(false);
  expect(auto.effective?.mode).not.toBe("log");
  const chosen = fleetY(w, null, { mode: "log", label: "log" } as never);
  expect(chosen.log).toBe(false);
});

test("axis label names the encoding: unit, member count and the three bands", () => {
  expect(fleetAxisLabel(d, "%")).toBe("% · spread across 100 members (bands: 25–75, 10–90, min–max)");
  expect(fleetAxisLabel(d, null)).toContain("value · spread across 100 members");
  expect(fleetAxisLabel({ ...d, normalise: "member" }, "%")).toMatch(/^× own median · /);
});

test("ribbons are one hue with strictly rising opacity, outermost lightest", () => {
  for (const dark of [false, true]) {
    const f = bandFills(dark);
    const alphas = f.map((c) => Number(c.match(/,([\d.]+)\)$/)![1]));
    expect(alphas).toEqual([...BAND_ALPHAS[dark ? "dark" : "light"]]);
    expect([...alphas].sort((a, b) => a - b)).toEqual(alphas);
    expect(new Set(f.map((c) => c.replace(/,[\d.]+\)$/, ""))).size).toBe(1);
  }
  expect(fleetKey(false).map((k) => k.id)).toEqual(["minmax", "q1090", "q2575", "median", "outlier"]);
});

test("median line clears 3:1 against the theme background", () => {
  const ratio = (a: string, b: string) => {
    const [x, y] = [relativeLuminance(a), relativeLuminance(b)].sort((p, q) => q - p);
    return (x + 0.05) / (y + 0.05);
  };
  expect(ratio(FLEET_HUE.light.line, "#ffffff")).toBeGreaterThanOrEqual(3);
  expect(ratio(FLEET_HUE.dark.line, "#16181d")).toBeGreaterThanOrEqual(3);
});

test("only isolated outlier samples get a dot; hover finds the nearest outlier", () => {
  expect(outlierMarkIdx(d, 0)).toEqual([]); // a continuous run is a line
  expect(outlierMarkIdx(d, 1)).toEqual([0, 2]); // 10, gap, 1: both isolated
  expect(outlierMarkIdx({ ...d, outliers: [{ ...d.outliers[0], values: [1, null, 2, 3] }] }, 0)).toEqual([0]);
  expect(nearestOutlier(d, 0, 17.5, 1)?.id).toBe("pod=a");
  expect(nearestOutlier(d, 0, 14, 1)).toBeNull();
  expect(nearestOutlier(d, 1, 10, 1)).toBeNull(); // pod=b has no value at step 1
});

test("end labels never overlap when outliers end at similar values (cis)", async () => {
  const { placeEndLabels } = await import("./fleet");
  const h = 12;
  // three labels at the right edge within 4 px of each other, one far away, one elsewhere in x
  const items = [
    { right: 500, w: 60, y: 100 }, { right: 500, w: 50, y: 103 }, { right: 500, w: 70, y: 98 },
    { right: 500, w: 60, y: 200 }, { right: 200, w: 60, y: 101 },
  ];
  const ys = placeEndLabels(items, h, 0, 300);
  const col = [0, 1, 2, 3].map((i) => ys[i]).sort((a, b) => a - b);
  for (let k = 1; k < col.length; k++) expect(col[k] - col[k - 1]).toBeGreaterThanOrEqual(h - 1e-9);
  expect(ys[2]).toBeLessThan(ys[0]); // order kept: the highest point keeps the highest label
  expect(ys[0]).toBeLessThan(ys[1]);
  expect(ys[3]).toBe(200); // nothing near it: untouched
  expect(ys[4]).toBe(101); // another x: not in the same column
});

test("end labels stay inside the plot, pushed back up from the bottom edge", async () => {
  const { placeEndLabels } = await import("./fleet");
  const ys = placeEndLabels([{ right: 10, w: 5, y: 295 }, { right: 10, w: 5, y: 296 }, { right: 10, w: 5, y: 299 }], 12, 0, 300);
  expect(Math.max(...ys)).toBeLessThanOrEqual(294);
  expect(ys[1] - ys[0]).toBeGreaterThanOrEqual(12 - 1e-9);
  expect(ys[2] - ys[1]).toBeGreaterThanOrEqual(12 - 1e-9);
});

test("outlier ends skip trailing gaps", async () => {
  const { outlierEnds } = await import("./fleet");
  expect(outlierEnds(d)).toEqual([{ j: 2, v: 20 }, { j: 2, v: 1 }]);
  expect(outlierEnds({ ...d, outliers: [{ ...d.outliers[0], values: [1, null, null] }, { ...d.outliers[0], values: [null, null, null] }] })).toEqual([{ j: 0, v: 1 }, null]);
});

const g: FleetData = {
  ...d,
  clusters: [
    { id: "c1", size: 60, band: { median: [10, 10, null], q25: [9, 9, null], q75: [11, 11, null] } },
    { id: "c2", size: 40, band: { median: [30, 31, 32], q25: [29, 30, 31], q75: [31, 32, 33] } },
  ],
  outliers: [{ ...d.outliers[0], cluster: "c2" }, d.outliers[1]],
  located: [
    { code: "untrusted_data", severity: "warn", message: "Data unknown", where: { spans: [[0, 300_000]], series: null }, source: "bucket_state" },
    { code: "members_missing", severity: "info", message: "m", where: { spans: [[300_000, 600_000]] }, source: "fleet" },
  ],
};

test("grouped fleet (oyi): whole min-max, then each group's IQR ribbon and median; outliers last", async () => {
  const { grouped } = await import("./fleet");
  expect(grouped(d)).toBe(false);
  expect(grouped({ ...d, clusters: [g.clusters![0]] })).toBe(false); // one group is the fleet
  expect(grouped(g)).toBe(true);
  const m = toFleetUplot(g);
  expect(m.roles).toEqual(["x", "lo", "hi", "cq25", "cq75", "cmedian", "cq25", "cq75", "cmedian", "outlier", "outlier"]);
  expect(m.bands.map((b) => b.series)).toEqual([[2, 1], [4, 3], [7, 6]]);
  expect(m.data[5]).toEqual([10, 10, null]); // c1 median: a gap stays a gap
  expect(m.data[8]).toEqual([30, 31, 32]);
});

test("grouped legend, key and axis label name the groups; outliers are tagged with theirs", async () => {
  const { groupEnds, untrustedSpans } = await import("./fleet");
  expect(fleetLegend(g)).toContain("2 behaviour groups: c1 (60), c2 (40)");
  expect(fleetLegend(g)).toContain("each judged within its group");
  expect(fleetAxisLabel(g, "s")).toContain("2 behaviour groups (per group: 25–75 band + median; all: min–max)");
  const key = fleetKey(false, g);
  expect(key.map((k) => k.id)).toEqual(["minmax", "group-c1", "group-c2", "outlier", "unknown"]);
  expect(key[1].line).toEqual({ color: "#14505B", style: "solid" });
  expect(key[2].line?.style).toBe("dashed");
  expect(key[4].hatch).toBe(true);
  expect(fleetKey(false, d).map((k) => k.id)).toEqual(["minmax", "q1090", "q2575", "median", "outlier"]);
  expect(outlierText(g.outliers[0], (ms) => `t${ms}`)).toBe("pod=a: consistently higher within group c2 since t0");
  expect(untrustedSpans(g)).toEqual([[0, 300_000]]); // only unknown data is hatched
  expect(groupEnds(g)).toEqual([{ j: 1, v: 10 }, { j: 2, v: 32 }]);
});

test("group styles clear 3:1 against the theme background and never reuse an outlier colour", async () => {
  const { GROUP_STYLES, OUTLIER_COLORS } = await import("./fleet");
  const contrast = (a: string, b: string) => {
    const [l1, l2] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
    return (l1 + 0.05) / (l2 + 0.05);
  };
  for (const s of GROUP_STYLES.light) expect(contrast(s.line, "#ffffff")).toBeGreaterThanOrEqual(3);
  for (const s of GROUP_STYLES.dark) expect(contrast(s.line, "#16181d")).toBeGreaterThanOrEqual(3);
  const all = [...GROUP_STYLES.light, ...GROUP_STYLES.dark].flatMap((s) => [s.line, s.fill].map((c) => c.toUpperCase()));
  for (const o of OUTLIER_COLORS) expect(all).not.toContain(o.toUpperCase());
});
