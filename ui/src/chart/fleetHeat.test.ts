import { expect, test } from "vitest";
import { relativeLuminance } from "./colormap";
import type { FleetData, FleetHeat } from "./fleet";
import { ABSENT, DIVERGING, GAP, binHeat, decimate, divergingLut, heatBarLabels, labelYs, lutIndex, rowPx, sharedRange } from "./fleetHeat";

const heat: FleetHeat = {
  z_cap: 6, rows_total: 2,
  rows: [
    { id: "a", z: [0.1, -4, 1, null, null, 0], rank: 0, first: 0, last: 5 },
    { id: "b", z: [null, null, 0, 0, 0, null], rank: null, first: 2, last: 4 },
  ],
};

test("lut: neutral centre, ends differ in hue (blue-content vs orange), lightness monotone away from centre", () => {
  for (const mode of ["light", "dark"] as const) {
    const lut = divergingLut(mode), at = (i: number) => `rgb(${lut[i * 3]},${lut[i * 3 + 1]},${lut[i * 3 + 2]})`;
    const lo = lut.slice(0, 3), hi = lut.slice(255 * 3);
    expect(lo[2]).toBeGreaterThan(lo[0]); // below is blue/purple
    expect(hi[0]).toBeGreaterThan(hi[2]); // above is orange
    const c = relativeLuminance(at(128));
    const sign = mode === "light" ? -1 : 1; // light: ends darker than the centre; dark: lighter
    for (const e of [0, 255]) expect(sign * (relativeLuminance(at(e)) - c)).toBeGreaterThan(0.1);
    expect(DIVERGING[mode]).toHaveLength(7);
  }
  expect(lutIndex(0)).toBe(128);
  expect(lutIndex(99)).toBe(255);
  expect(lutIndex(-99)).toBe(0);
});

test("binning keeps the most extreme z; gaps while alive differ from absent outside the lifetime", () => {
  const g = binHeat(heat, 6, 3); // bins of 2 columns
  expect([g.z[0], g.z[1], g.z[2]]).toEqual([-4, 1, 0]);
  const g1 = binHeat(heat, 6, 99);
  expect(Array.from(g1.kind.slice(0, 6))).toEqual([0, 0, 0, GAP, GAP, 0]);
  expect(Array.from(g1.kind.slice(6, 12))).toEqual([ABSENT, ABSENT, 0, 0, 0, ABSENT]);
});

test("rows: at least the budget's 4 px, labels stay apart", () => {
  expect(rowPx(100)).toBe(4);
  expect(rowPx(5)).toBe(14);
  expect(labelYs([0, 1, 2], 4)).toEqual([6, 18, 30]);
});

test("decimate keeps band extremes and the outlier's peak", () => {
  const n = 100;
  const x = Array.from({ length: n }, (_, i) => i);
  const flat = x.map(() => 10 as number | null);
  const out = x.map((i) => (i === 51 ? 90 : 12) as number | null);
  const hi = x.map((i) => (i === 51 ? 95 : 20) as number | null);
  const d = decimate([x, flat, hi, flat, out], ["x", "lo", "hi", "median", "outlier"], 10);
  expect(d[0]).toHaveLength(10);
  expect(Math.max(...(d[2] as number[]))).toBe(95);
  expect(Math.max(...(d[4] as number[]))).toBe(90);
  expect(decimate([x, flat], ["x", "median"], 200)[0]).toHaveLength(n);
});

test("shared y covers the envelope and every drawn outlier", () => {
  const d = {
    band: { lo: [1, null], hi: [5, 6], median: [], q25: [], q75: [], q10: [], q90: [] },
    outliers: [{ values: [2, 30] }, { values: [-4, 1] }],
  } as unknown as FleetData;
  const [lo, hi] = sharedRange(d);
  expect(lo).toBeLessThan(-4);
  expect(hi).toBeGreaterThan(30);
  expect(sharedRange(d, 1)[0]).toBeLessThan(1);
  expect(sharedRange(d, 1)[0]).toBeGreaterThan(-4);
});

test("heat colour bar is labelled as a deviation from the fleet median, centred on 0", () => {
  expect(heatBarLabels(6)).toEqual({ title: "deviation from fleet median (σ)", lo: "−6", mid: "0", hi: "+6" });
});
