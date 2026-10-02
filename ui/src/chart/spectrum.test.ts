import { expect, test } from "vitest";
import { layoutPeakLabels, limitZones, peakMarks, toSpectrumUplot } from "./spectrum";

const s = (id: string, periods: number[], power: number[]) => ({
  id, labels: { job: id }, periods_s: periods, power, level: 0.2,
  peaks: [{ period_s: 300, interval_s: [290, 310] as [number, number], power: 0.8, significant: true, fap: 1e-9 },
          { period_s: 90, interval_s: [88, 92] as [number, number], power: 0.1, significant: false, fap: 0.4 }],
  caveats: [],
});
const d = { series: [s("a", [100, 300, 900], [0.1, 0.8, 0.2])], limits: { shortest_s: 240, longest_s: 172800 } };

test("period-axis model: one line per series plus the false-alarm level", () => {
  const m = toSpectrumUplot(d);
  expect(m.data[0]).toEqual([100, 300, 900]);
  expect(m.data[1]).toEqual([0.1, 0.8, 0.2]);
  expect(m.data[2]).toEqual([0.2, 0.2, 0.2]);
  expect(String(m.series[2].label)).toContain("false-alarm");
  expect(m.xRange[0]).toBeCloseTo(150);
});
test("red-noise level drawn per series when present", () => {
  const r = { ...d, series: [{ ...d.series[0], red_level: [0.05, 0.1, 0.3], ar1_phi: 0.4 }] };
  const m = toSpectrumUplot(r);
  expect(m.data[3]).toEqual([0.05, 0.1, 0.3]);
  expect(String(m.series[3].label)).toContain("red noise");
});
test("limits are hatched, only significant peaks are marked", () => {
  expect(limitZones(d.limits, 100, 300000).map((z) => [z.from, z.to])).toEqual([[100, 240], [172800, 300000]]);
  expect(peakMarks(d.series[0]).map((p) => p.text)).toEqual(["5m [4.8m–5.2m]"]);
});

const idPx = (x: number) => x; // identity: treat input x as already in pixel space

test("widely spaced labels keep their full text on one row", () => {
  const marks = [
    { x: 0, text: "1h", shortText: "1h", power: 10 },
    { x: 500, text: "54.1m [53.0m-55.2m]", shortText: "54.1m", power: 1 },
  ];
  const out = layoutPeakLabels(marks, idPx);
  expect(out.find((m) => m.x === 0)!.text).toBe("1h");
  expect(out.find((m) => m.x === 500)!.text).toBe("54.1m [53.0m-55.2m]");
  expect(out.every((m) => m.row === 0)).toBe(true);
});

test("a secondary label whose full text would run into the next peak is shortened, keeping one row", () => {
  const marks = [
    { x: 0, text: "1h", shortText: "1h", power: 10 }, // dominant
    { x: 16, text: "54.1m [53.0m-55.2m]", shortText: "54.1m", power: 1 }, // full text overlaps peak at 60
    { x: 60, text: "56.7m", shortText: "56.7m", power: 1 },
  ];
  const out = layoutPeakLabels(marks, idPx);
  expect(out.find((m) => m.x === 16)!.text).toBe("54.1m"); // CI text dropped
  expect(out.every((m) => m.row === 0)).toBe(true); // shortening was enough, no stacking needed
});

test("the dominant peak always keeps its full label; a label that still collides is stacked onto the next row", () => {
  const marks = [
    { x: 0, text: "1h", shortText: "1h", power: 10 }, // dominant
    { x: 5, text: "56.7m [55.6m-57.8m]", shortText: "56.7m", power: 1 }, // too close even after shortening would help
  ];
  const out = layoutPeakLabels(marks, idPx);
  const dominant = out.find((m) => m.x === 0)!;
  const secondary = out.find((m) => m.x === 5)!;
  expect(dominant.text).toBe("1h");
  expect(dominant.row).toBe(0);
  expect(secondary.row).toBe(1); // pushed to a new row instead of overlapping the dominant label
});

test("extra fields (e.g. a series index) round-trip through the layout", () => {
  const marks = [
    { x: 0, text: "1h", shortText: "1h", power: 10, k: 0, id: "0:0" },
    { x: 500, text: "5m", shortText: "5m", power: 1, k: 1, id: "1:0" },
  ];
  const out = layoutPeakLabels(marks, idPx);
  expect(out.map((m) => m.id).sort()).toEqual(["0:0", "1:0"]);
  expect(out.find((m) => m.id === "1:0")!.k).toBe(1);
});
