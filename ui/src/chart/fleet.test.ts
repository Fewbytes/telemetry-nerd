import { expect, test } from "vitest";
import { relativeLuminance } from "./colormap";
import { BAND_ALPHAS, FLEET_HUE, bandFills, fleetAxisLabel, fleetKey, nearestOutlier, outlierMarkIdx, fleetY, coverageGaps, fleetLegend, outlierText, toFleetUplot, type FleetData, type FleetSpc } from "./fleet";

const d: FleetData = {
  ts: [0, 300_000, 600_000], members: 100, normalise: "none", scale: "log",
  band: {
    median: [10, 11, null], q25: [9, 10, null], q75: [11, 12, null], q10: [8, 9, null],
    q90: [12, 13, null], lo: [5, 6, 7], hi: [20, 21, 22],
  },
  n: [100, 97, 2], alive: [100, 100, 100], outlier_count: 8,
  outliers: [
    { id: "pod=a", labels: { pod: "a" }, kind: "persistent", direction: "higher", score: 3, values: [18, 19, 20], since_ms: 0, since_window_start: true, episodes: [],
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
  expect(s).not.toContain("min–max"); // the encoding is in the key, once
  expect(fleetLegend({ ...d, outliers: [], outlier_count: 0, normalise: "member" })).toContain("no outliers");
  expect(fleetLegend({ ...d, normalise: "member" })).toContain("relative to its own median");
});

test("outlier text says kind, direction, effect or episode; coverage gaps are shares of alive", () => {
  const t = (ms: number) => `t${ms / 1000}`;
  expect(outlierText(d.outliers[0], t)).toBe("pod=a: consistently higher · +38% since the time range start");
  expect(outlierText(d.outliers[1], t)).toBe("pod=b: briefly lower · spike 6.1σ t600 (momentary)");
  // spec §5.4: special cause is the default reading; an undetermined source is said
  expect(outlierText({ ...d.outliers[1], source: "undetermined" }, t)).toBe("pod=b: briefly lower · spike 6.1σ t600 (momentary) (source undetermined)");
  expect(fleetKey(false, d, "quantiles")[0].title).toContain("descriptive: these members only");
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
  expect(fleetKey(false, d).map((k) => k.id)).toEqual(["minmax", "q1090", "q2575", "median", "outlier", "transient"]);
  expect(outlierText(g.outliers[0], (ms) => `t${ms}`)).toBe("pod=a: consistently higher within group c2 · +38% since the time range start");
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
const zones = (k: number): FleetSpc => ({
  centre: [10 * k, 11 * k, 12 * k], lo2: [8 * k, 9 * k, 10 * k], hi2: [12.5 * k, 13.4 * k, 14.4 * k], lo3: [7 * k, 8 * k, 9 * k], hi3: [14 * k, 15 * k, 16 * k],
  threshold_z: 5.86, threshold_lo: [5 * k, 6 * k, 7 * k], threshold_hi: [20 * k, 21 * k, 22 * k], threshold_note: "approximate: …",
  window: 13, pool_half: 6, legend: "median ± 2σ/3σ (robust, pooled ±6 steps, log scale: multiplicative)",
  outside3_total: 2, outside3_expected: 0.8, outside3_rate: 2 / 297, outside3_cells: 297, heavy_tails: false,
  outside3_count: "2 member-steps beyond 3σ unflagged (0.67%; 0.27% if normal)",
  outside3_note: "outside 3σ, not significant at fleet-wide 1% (100 members tested)",
  widening: [1], widening_note: "more members beyond 3σ at this step than chance gives at 99% …", tested: 100,
});
const spc: FleetData = {
  ...d,
  spc: zones(1),
  member_error_note: "member measurement error not propagated (principle 4): …",
  band_bounds: { steps: [1], missing: [3], median_lo: [10.5], median_hi: [11.4], q25_lo: [null], q25_hi: [10.2] },
};

test("SPC view layers: ±3σ outer, ±2σ inner, median centre, dashed flag bar both sides, then outliers", async () => {
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
  const untested = { ...spc, spc: { ...zones(1), threshold_z: null, threshold_lo: null, threshold_hi: null } };
  expect(toFleetUplot(untested, "spc").roles).not.toContain("thrlo");
  expect(fleetKey(false, untested).map((k) => k.id)).not.toContain("flag");
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
  const ratio = (a: string, b: string) => {
    const [x, y] = [relativeLuminance(a), relativeLuminance(b)].sort((p, q) => q - p);
    return (x + 0.05) / (y + 0.05);
  };
  expect(ratio(MUTED_LINE.light, "#ffffff")).toBeGreaterThanOrEqual(3);
  expect(ratio(MUTED_LINE.dark, "#16181d")).toBeGreaterThanOrEqual(3);
});

test("both modes: a level outlier with episodes stays one coloured line; its brackets are unlabelled (uncalibrated)", async () => {
  const { modeText } = await import("./fleet");
  const both = { ...spc.outliers[0], episodes: [{ start_ms: 300_000, end_ms: 600_000, peak_z: 4.04, sustained: true, beyond_own_level: true }] };
  const m = toFleetUplot({ ...spc, outliers: [both] }, "spc");
  expect(m.roles.filter((r) => ["outlier", "muted", "episode"].includes(r))).toEqual(["outlier"]);
  const t = (ms: number) => `t${ms / 1000}`;
  expect(modeText(both, t)).toBe("+38% since the time range start"); // no σ, no episode text
  const key = fleetKey(false, { ...spc, outliers: [both] }).map((k) => k.id);
  expect(key).toContain("own-level");
  expect(key).not.toContain("transient"); // no transient member drawn
});

test("end-label mode text: kind + effect for level / change outliers, the strongest episode for transients", async () => {
  const { modeText, effectText } = await import("./fleet");
  const t = (ms: number) => `t${ms / 1000}`;
  const base = spc.outliers[0];
  expect(modeText({ ...base, since_window_start: false }, t)).toBe("+38% (time-range mean) since t0");
  expect(modeText(base, t)).toBe("+38% since the time range start");
  expect(modeText({ ...base, since_window_start: false, since_ms: 300_000, effect: { as: "ratio", offset: 1.5, over: "since" } }, t)).toBe("+50% since t300");
  expect(modeText({ ...base, kind: "shifted", effect: { as: "ratio", offset: 1.2, change: 1.38, at_ms: 300_000 } }, t)).toBe("shifted +38% at t300");
  expect(modeText({ ...base, kind: "drifting", effect: { as: "ratio", offset: 1.1, change: 1.5, change_per_hour: 1.021 } }, t)).toBe("drifting +2.1%/h");
  expect(modeText({ ...base, effect: { as: "difference", offset: 12.34 } }, t, "ms")).toBe("+12.3 ms since the time range start");
  const two = { ...spc.outliers[1], episodes: [spc.outliers[1].episodes[0], { start_ms: 0, end_ms: 300_000, peak_z: 7.2, sustained: true, beyond_own_level: false }] };
  expect(modeText(two, t)).toBe("episode 7.2σ t0–t300 (sustained) +1 more");
  expect(effectText(2.7, "ratio")).toBe("×2.7");
  expect(effectText(0.4, "ratio")).toBe("÷2.5");
  expect(effectText(0.88, "ratio")).toBe("−12%");
});

test("the key carries the encoding (flag bar explained on hover); the legend only counts and caveats", () => {
  const key = fleetKey(false, spc, "spc");
  expect(key.map((k) => k.id)).toEqual(["z3", "z2", "median", "flag", "widening", "outlier", "transient"]);
  const flag = key.find((k) => k.id === "flag")!;
  expect(flag.label).toBe("flag bar 5.9σ (approximate)");
  expect(flag.title).toMatch(/per-member bar varies with pooling.*leave-one-out σ up to 64 members/);
  expect(key.find((k) => k.id === "z3")!.title).toContain("median ± 2σ/3σ");
  const s = fleetLegend(spc, "spc");
  expect(s).toBe("100 members · 8 outliers (2 drawn) (special causes) · 2–100 reporting per step · 2 member-steps beyond 3σ unflagged (0.67%; 0.27% if normal) · 1 step where the fleet widened faster than the ±6-step σ tracks (common cause) · member measurement error not propagated");
  expect(fleetLegend({ ...spc, spc: { ...zones(1), heavy_tails: true } })).toContain("(0.67%; 0.27% if normal); heavy-tailed noise: more beyond 3σ is this fleet's shape");
  expect(key.find((k) => k.id === "widening")!.title).toContain("Chance of any mark in this time range ≈ 1%");
  expect(s).not.toMatch(/2σ\/3σ|dashed/); // no encoding twice
  expect(fleetAxisLabel(spc, "%", "spc")).toBe("% · median ± 2σ/3σ (robust, pooled ±6 steps, log scale: multiplicative) across 100 members");
  expect(fleetLegend(spc, "quantiles")).toContain("1 steps with members missing (bounds drawn) · member measurement error not propagated");
});

test("toggle: the quantile view keeps the descriptive band and adds sparse missing-member bounds", async () => {
  const { bandView, boundsAt, boundsText, missingSteps } = await import("./fleet");
  expect(bandView(spc, "spc")).toBe("spc");
  expect(bandView(spc, "quantiles")).toBe("quantiles");
  expect(bandView(d, "spc")).toBe("quantiles"); // no zones in the payload
  expect(toFleetUplot(spc, "quantiles").roles.slice(0, 8)).toEqual(["x", "lo", "hi", "q10", "q90", "q25", "q75", "median"]);
  expect(fleetKey(false, spc, "quantiles").map((k) => k.id)).toContain("bounds");
  expect(missingSteps(spc)).toEqual([1]); // the payload's steps (stale-marked members already out)
  expect(missingSteps(d)).toEqual([]); // no bounds on the wire: nobody missing
  expect(boundsAt(spc, 0)).toEqual([]);
  expect(boundsAt(spc, 1)).toEqual(expect.arrayContaining([
    { q: "median", lo: 10.5, hi: 11.4 }, { q: "q25", lo: -Infinity, hi: 10.2 },
  ]));
  const txt = boundsText(spc, 1, (ms) => `t${ms / 1000}`)!;
  expect(txt).toContain("3 alive members missing (97 reporting)");
  expect(txt).toContain("25% unbounded–10.2");
  expect(txt).toContain("min / max: unknown beyond");
});

test("a drawn transient's step beyond 3σ outside its episodes is not significant; widening marks per step", async () => {
  const { outsideUnflagged, wideningMarks } = await import("./fleet");
  const o = { ...spc.outliers[1], values: [20, null, 1] }; // step 0 beyond +3σ (14), not in the episode
  expect(outsideUnflagged(spc, o, 0)).toBe(true);
  expect(outsideUnflagged(spc, o, 2)).toBe(false); // inside the episode: flagged
  expect(outsideUnflagged(spc, spc.outliers[0], 0)).toBe(false); // level outliers are flagged as a whole
  expect(wideningMarks(spc)).toEqual([{ x: 300, j: 1, k: null }]);
});

test("grouped SPC: each group's own zones and flag bar, no whole-fleet band; hover uses the outlier's group", async () => {
  const { bandView, outsideUnflagged, wideningMarks, groupEnds } = await import("./fleet");
  const gz: FleetData = { ...g, clusters: g.clusters!.map((c, k) => ({ ...c, spc: { ...zones(k + 1), widening: k ? [2] : [] } })) };
  expect(bandView(gz, "spc")).toBe("spc");
  const m = toFleetUplot(gz, "spc");
  expect(m.roles.slice(0, 15)).toEqual(["x", "lo3", "hi3", "lo2", "hi2", "cmedian", "thrlo", "thrhi", "lo3", "hi3", "lo2", "hi2", "cmedian", "thrlo", "thrhi"]);
  expect(m.bands.map((b) => b.series)).toEqual([[2, 1], [4, 3], [9, 8], [11, 10]]);
  expect(groupEnds(gz)).toEqual([{ j: 2, v: 12 }, { j: 2, v: 24 }]);
  expect(fleetKey(false, gz).map((k) => k.id)).toEqual(["group-c1", "group-c2", "flag", "widening", "outlier", "transient", "unknown"]);
  expect(fleetLegend(gz)).toContain("4 member-steps beyond 3σ unflagged (0.67%; 0.27% if normal) · 1 step where the group widened");
  // pod=a is judged in c2 (zones x2): 30 is beyond c2's +3σ (28) only as a transient would be
  expect(outsideUnflagged(gz, { ...gz.outliers[0], kind: "transient", values: [30, 0, 0] }, 0)).toBe(true);
  expect(wideningMarks(gz)).toEqual([{ x: 600, j: 2, k: 1 }]);
  expect(bandView(g, "spc")).toBe("quantiles"); // groups without zones: quantiles
});

test("decimation keeps zone edges as extremes and the transient's segments; grouped columns do not crash", async () => {
  const { decimate } = await import("./fleetHeat");
  const n = 40, x = Array.from({ length: n }, (_, i) => i), c = x.map(() => 10);
  const hi3 = x.map((i) => (i === 7 ? 30 : 15)), ep = x.map((i) => (i === 9 ? 50 : null));
  const out = decimate([x, c, hi3, ep], ["x", "median", "hi3", "episode"], 4);
  expect(Math.max(...(out[2] as number[]))).toBe(30);
  expect(out[3]).toContain(50);
  const grp = decimate([x, c, hi3, ep], ["x", "cmedian", "hi3", "outlier"], 4);
  expect(grp[3]).toContain(50);
  expect(decimate([x, ep], ["x", "outlier"], 4)[1]).toContain(50); // no centre at all
});

test("x ticks are 24 h UTC: 'UTC' on the first, the date where the day changes", async () => {
  const { utcTicks } = await import("./fleet");
  const t = (iso: string) => Date.parse(iso) / 1000;
  expect(utcTicks([t("2026-10-03T22:00:00Z"), t("2026-10-03T23:00:00Z"), t("2026-10-04T00:00:00Z"), t("2026-10-04T01:30:00Z")]))
    .toEqual(["22:00 UTC", "23:00", "10-04 00:00", "01:30"]);
});
