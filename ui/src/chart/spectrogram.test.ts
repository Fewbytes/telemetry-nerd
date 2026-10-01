import { expect, test } from "vitest";
import { layoutSpectrogram } from "./spectrogram";

test("columns centred, missing windows hatched, non-significant cells faint", () => {
  // power is [row][column]; the middle column has no data at all
  const s = {
    id: "a", labels: {}, ts: [1800e3, 3600e3, 5400e3],
    rows: { lo_s: [60, 120], hi_s: [120, 240] },
    power: [[0.9, null, 0.05], [0.1, null, 0.02]],
    level: [0.2, null, 0.2],
  };
  const l = layoutSpectrogram(s, { width: 300, height: 100, startMs: 0, endMs: 7200e3, hopMs: 1800e3 });
  expect(l.missing).toHaveLength(1);
  expect(l.rects).toHaveLength(4);
  expect(l.rects.filter((r) => r.faint)).toHaveLength(3); // only 0.9 clears the 0.2 level
  expect(l.rects[0].t).toBeCloseTo(0.9); // linear 0..1: share of variance, comparable across columns
});
