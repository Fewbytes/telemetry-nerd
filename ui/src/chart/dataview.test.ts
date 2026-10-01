import { expect, test } from "vitest";
import { drawnFor } from "./dataview";
import type { SeriesData, TimePanelData } from "../lib/api";

const s = (id: string, v: number): SeriesData => ({ id, labels: {}, ts: [0], avg: [v], min: [v], max: [v], count: [1] });
const d = { series: [s("f", 1)], raw: [s("r", 2)], removed: [s("x", 3)] } as unknown as TimePanelData;

test("each data view maps to drawn series and context", () => {
  expect(drawnFor("filtered", d).series[0].id).toBe("f");
  expect(drawnFor("raw", d).series[0].id).toBe("r");
  expect(drawnFor("overlay", d)).toMatchObject({ series: [{ id: "f" }], context: { role: "raw", series: [{ id: "r" }] } });
  expect(drawnFor("removed", d)).toMatchObject({ series: [{ id: "r" }], context: { role: "removed", series: [{ id: "x" }] } });
});
