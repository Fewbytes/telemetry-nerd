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
    { id: "pod=a", labels: { pod: "a" }, kind: "persistent", direction: "higher", score: 3, values: [18, 19, 20], since_ms: 0, episodes: [],
      effect: { as: "ratio", offset: 1.38 } },
    { id: "pod=b", labels: { pod: "b" }, kind: "transient", direction: "lower", score: 2, values: [10, null, 1], since_ms: null,
      episodes: [{ start_ms: 600_000, end_ms: 600_000, peak_z: -6.13, sustained: false, beyond_own_level: false }], effect: { as: "ratio", offset: 0.97 } },
  ],
};

test("quantile view columns: nested band fills, median, then drawn outliers; gaps stay null", () => {
  const m = toFleetUplot(d, "quantiles");
  expect(m.roles).toEqual(["x", "lo", "hi", "q10", "q90", "q25", "q75", "median", "outlier", "muted", "episode"]);
  expect(m.data[0]).toEqual([0, 300, 600]);
  expect(m.data[7]).toEqual([10, 11, null]);
  expect(m.data[9]).toEqual([10, null, 1]);
  expect(m.bands.map((b) => b.series)).toEqual([[2, 1], [4, 3], [6, 5]]);
  // no SPC zones in the payload: the SPC view falls back to the quantiles
  expect(toFleetUplot(d, "spc").roles).toEqual(m.roles);
});

test("quantile legend states group size, outliers drawn of found, n per step and the band", () => {
  const s = fleetLegend(d, "quantiles");
  expect(s).toContain("100 members · 8 outliers (2 drawn)");
  expect(s).toContain("2–100 reporting per step");
  expect(s).toContain("min–max, 10–90%, 25–75%");
  expect(fleetLegend({ ...d, outliers: [], outlier_count: 0, normalise: "member" })).toContain("no outliers");
  expect(fleetLegend({ ...d, normalise: "member" })).toContain("relative to its own median");
});

test("outlier text says kind, direction, effect or episode; coverage gaps are shares of alive", () => {
  const t = (ms: number) => `t${ms / 1000}`;
  expect(outlierText(d.outliers[0], t)).toBe("pod=a: consistently higher · +38% since t0");
  expect(outlierText(d.outliers[1], t)).toBe("pod=b: briefly lower · spike 6.1σ t600 (momentary)");
  // spec §5.4: special cause is the default reading; an undetermined source is said
  expect(outlierText({ ...d.outliers[1], source: "undetermined" }, t)).toBe("pod=b: briefly lower · spike 6.1σ t600 (momentary) (source undetermined)");
  expect(fleetLegend(d, "quantiles")).toContain("descriptive: these members only");
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
  expect(fleetAxisLabel(d, "%", "quantiles")).toBe("% · spread across 100 members (bands: 25–75, 10–90, min–max)");
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
  expect(m.roles).toEqual(["x", "lo", "hi", "cq25", "cq75", "cmedian", "cq25", "cq75", "cmedian", "outlier", "muted", "episode"]);
  expect(m.bands.map((b) => b.series)).toEqual([[2, 1], [4, 3], [7, 6]]);
  expect(m.data[5]).toEqual([10, 10, null]); // c1 median: a gap stays a gap
  expect(m.data[8]).toEqual([30, 31, 32]);
});

test("grouped legend, key and axis label name the groups; outliers are tagged with theirs", async () => {
  const { groupEnds, untrustedSpans } = await import("./fleet");
  expect(fleetLegend(g)).toContain("2 behaviour groups (systemic structure): c1 (60), c2 (40)");
  expect(fleetLegend(g)).toContain("each judged within its group");
  expect(fleetAxisLabel(g, "s")).toContain("2 behaviour groups (per group: 25–75 band + median; all: min–max)");
  const key = fleetKey(false, g);
  expect(key.map((k) => k.id)).toEqual(["minmax", "group-c1", "group-c2", "outlier", "transient", "unknown"]);
  expect(key[1].line).toEqual({ color: "#14505B", style: "solid" });
  expect(key[2].line?.style).toBe("dashed");
  expect(key[5].hatch).toBe(true);
  expect(fleetKey(false, d).map((k) => k.id)).toEqual(["minmax", "q1090", "q2575", "median", "bounds", "outlier", "transient"]);
  expect(outlierText(g.outliers[0], (ms) => `t${ms}`)).toBe("pod=a: consistently higher within group c2 · +38% since t0");
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

// SPC band (nq6): the reference the outlier tests use, distinct marks per outlier mode, quantile toggle
const spc: FleetData = {
  ...d,
  spc: {
    centre: [10, 11, 12], lo2: [8, 9, 10], hi2: [12.5, 13.4, 14.4], lo3: [7, 8, 9], hi3: [14, 15, 16],
    threshold_z: 5.86, threshold_lo: [5, 6, 7], threshold_hi: [20, 21, 22],
    window: 13, pool_half: 6, legend: "median ± 2σ/3σ (robust, pooled ±6 steps, log scale: multiplicative)",
    outside3: [0, 2, 0], outside3_note: "outside 3σ, not significant at fleet-wide 1% (100 members tested)", tested: 100,
    note: "member measurement error not propagated (principle 4): …",
  },
  band_bounds: { median_lo: [10, 10.5, null], median_hi: [10, 11.4, null], q25_lo: [9, null, null], q25_hi: [9, 10.2, null] },
};

test("SPC view layers: ±3σ outer, ±2σ inner, median centre, dashed flag threshold both sides, then outliers", async () => {
  const { THRESHOLD_DASH, ZONE_ALPHAS } = await import("./fleet");
  const m = toFleetUplot(spc, "spc");
  expect(m.roles).toEqual(["x", "lo3", "hi3", "lo2", "hi2", "median", "thrlo", "thrhi", "outlier", "muted", "episode"]);
  expect(m.bands.map((b) => b.series)).toEqual([[2, 1], [4, 3]]);
  expect(m.data[5]).toEqual([10, 11, 12]);
  expect(m.data[6]).toEqual([5, 6, 7]);
  expect(m.data[7]).toEqual([20, 21, 22]);
  expect(THRESHOLD_DASH.length).toBe(2);
  const f = bandFills(false, "spc").map((c) => Number(c.match(/,([\d.]+)\)$/)![1]));
  expect(f).toEqual([...ZONE_ALPHAS.light]);
  expect(f[0]).toBeLessThan(f[1]); // outer zone lighter
  // no tests ran: no threshold columns
  const none = toFleetUplot({ ...spc, spc: { ...spc.spc!, threshold_z: null, threshold_lo: null, threshold_hi: null } }, "spc");
  expect(none.roles).not.toContain("thrlo");
  expect(fleetKey(false, { ...spc, spc: { ...spc.spc!, threshold_z: null, threshold_lo: null, threshold_hi: null } }).map((k) => k.id)).not.toContain("flag");
});

test("a transient is a grey line with only its episode steps coloured; a level outlier is one coloured line", async () => {
  const { episodeValues, MUTED_LINE } = await import("./fleet");
  const m = toFleetUplot(spc, "spc");
  const at = (r: string) => m.roles.indexOf(r);
  expect(m.data[at("outlier")]).toEqual([18, 19, 20]);
  expect(m.owner[at("outlier")]).toBe(0);
  expect(m.data[at("muted")]).toEqual([10, null, 1]); // the whole member, drawn grey
  expect(m.data[at("episode")]).toEqual([null, null, 1]); // only inside its episode
  expect(m.owner[at("muted")]).toBe(1);
  expect(m.owner[at("episode")]).toBe(1);
  const wide = { ...spc.outliers[1], values: [3, 4, 5], episodes: [{ start_ms: 300_000, end_ms: 600_000, peak_z: 4, sustained: true, beyond_own_level: false }] };
  expect(episodeValues(spc, wide)).toEqual([null, 4, 5]);
  // the muted line still clears 3:1 on the theme background
  const ratio = (a: string, b: string) => {
    const [x, y] = [relativeLuminance(a), relativeLuminance(b)].sort((p, q) => q - p);
    return (x + 0.05) / (y + 0.05);
  };
  expect(ratio(MUTED_LINE.light, "#ffffff")).toBeGreaterThanOrEqual(3);
  expect(ratio(MUTED_LINE.dark, "#16181d")).toBeGreaterThanOrEqual(3);
});

test("both modes: a level outlier with episodes stays one coloured line and its label names both", async () => {
  const { modeText } = await import("./fleet");
  const both = { ...spc.outliers[0], episodes: [{ start_ms: 300_000, end_ms: 600_000, peak_z: 4.04, sustained: true, beyond_own_level: true }] };
  const m = toFleetUplot({ ...spc, outliers: [both] }, "spc");
  expect(m.roles.filter((r) => ["outlier", "muted", "episode"].includes(r))).toEqual(["outlier"]);
  const t = (ms: number) => `t${ms / 1000}`;
  expect(modeText(both, t)).toBe("+38% since t0 · episode 4σ beyond own level t300–t600 (sustained)");
  expect(fleetKey(false, { ...spc, outliers: [both] }).map((k) => k.id)).toContain("transient"); // bracket encoding explained
});

test("end-label mode text: kind + effect for level / change outliers, the strongest episode for transients", async () => {
  const { modeText, effectText } = await import("./fleet");
  const t = (ms: number) => `t${ms / 1000}`;
  const base = spc.outliers[0];
  expect(modeText({ ...base, kind: "shifted", effect: { as: "ratio", offset: 1.2, change: 1.38, at_ms: 300_000 } }, t)).toBe("shifted +38% at t300");
  expect(modeText({ ...base, kind: "drifting", effect: { as: "ratio", offset: 1.1, change: 1.5, change_per_hour: 1.021 } }, t)).toBe("drifting +2.1%/h");
  expect(modeText({ ...base, effect: { as: "difference", offset: 12.34 } }, t, "ms")).toBe("+12.3 ms since t0");
  const two = { ...spc.outliers[1], episodes: [spc.outliers[1].episodes[0], { start_ms: 0, end_ms: 300_000, peak_z: 7.2, sustained: true, beyond_own_level: false }] };
  expect(modeText(two, t)).toBe("episode 7.2σ t0–t300 (sustained) +1 more");
  expect(effectText(2.7, "ratio")).toBe("×2.7");
  expect(effectText(0.4, "ratio")).toBe("÷2.5");
  expect(effectText(0.88, "ratio")).toBe("−12%");
});

test("SPC legend, key and axis label state the band, the threshold, the rug and the error caveat", () => {
  const s = fleetLegend(spc, "spc");
  expect(s).toContain("zones: median ± 2σ/3σ (robust, pooled ±6 steps, log scale: multiplicative)");
  expect(s).toContain("dashed: flag threshold 5.9σ (single step, fleet-wide 1%");
  expect(s).toContain("top ticks: outside 3σ, not significant at fleet-wide 1% (100 members tested)");
  expect(s).toContain("member measurement error not propagated");
  expect(s).toContain("grey line with coloured, bracketed segments: transient episodes");
  expect(fleetKey(false, spc, "spc").map((k) => k.id)).toEqual(["z3", "z2", "median", "flag", "rug", "outlier", "transient"]);
  expect(fleetAxisLabel(spc, "%", "spc")).toBe("% · median ± 2σ/3σ (robust, pooled ±6 steps, log scale: multiplicative) across 100 members");
  expect(fleetLegend(spc, "quantiles")).toContain("member measurement error not propagated");
});

test("toggle: the quantile view keeps the descriptive band and adds missing-member bounds", async () => {
  const { bandView, boundsAt, boundsText, missingSteps } = await import("./fleet");
  expect(bandView(spc, "spc")).toBe("spc");
  expect(bandView(spc, "quantiles")).toBe("quantiles");
  expect(bandView(d, "spc")).toBe("quantiles"); // no zones in an older payload
  expect(toFleetUplot(spc, "quantiles").roles.slice(0, 8)).toEqual(["x", "lo", "hi", "q10", "q90", "q25", "q75", "median"]);
  expect(fleetKey(false, spc, "quantiles").map((k) => k.id)).toContain("bounds");
  expect(fleetLegend(spc, "quantiles")).toContain("lighter: missing-member bounds");
  expect(missingSteps(spc)).toEqual([1, 2]);
  expect(boundsAt(spc, 0)).toEqual([]); // nobody missing: no bounds drawn
  // step 1: 97 of 100; q25's lower bound reaches the missing ranks: unbounded
  expect(boundsAt(spc, 1)).toEqual(expect.arrayContaining([
    { q: "median", lo: 10.5, hi: 11.4 }, { q: "q25", lo: -Infinity, hi: 10.2 },
  ]));
  const txt = boundsText(spc, 1, (ms) => `t${ms / 1000}`)!;
  expect(txt).toContain("97 of 100 alive reporting");
  expect(txt).toContain("25% unbounded–10.2");
  expect(txt).toContain("min / max: unknown beyond");
});

test("a transient's step beyond 3σ outside its episodes is said to be not significant; flagged steps are not", async () => {
  const { outsideUnflagged, outsideRug } = await import("./fleet");
  const o = { ...spc.outliers[1], values: [20, null, 1] }; // step 0 beyond +3σ (14), not in the episode
  expect(outsideUnflagged(spc, o, 0)).toBe(true);
  expect(outsideUnflagged(spc, o, 2)).toBe(false); // inside the episode: flagged
  expect(outsideUnflagged(spc, spc.outliers[0], 0)).toBe(false); // level outliers are flagged as a whole
  expect(outsideRug(spc)).toEqual([{ x: 300, j: 1, count: 2 }]);
});

test("decimation keeps zone edges as extremes and the transient's segments as lines", async () => {
  const { decimate } = await import("./fleetHeat");
  const n = 40, x = Array.from({ length: n }, (_, i) => i), c = x.map(() => 10);
  const hi3 = x.map((i) => (i === 7 ? 30 : 15)), ep = x.map((i) => (i === 9 ? 50 : null));
  const out = decimate([x, c, hi3, ep], ["x", "median", "hi3", "episode"], 4);
  expect(Math.max(...(out[2] as number[]))).toBe(30);
  expect(out[3]).toContain(50);
});
