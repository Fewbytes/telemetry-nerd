import { expect, test } from "vitest";
import { limitZones, peakMarks, toSpectrumUplot } from "./spectrum";

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
test("limits are hatched, only significant peaks are marked", () => {
  expect(limitZones(d.limits, 100, 300000).map((z) => [z.from, z.to])).toEqual([[100, 240], [172800, 300000]]);
  expect(peakMarks(d.series[0]).map((p) => p.text)).toEqual(["5m [4.8m–5.2m]"]);
});
