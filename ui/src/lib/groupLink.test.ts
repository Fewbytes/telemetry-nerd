import { describe, expect, it } from "vitest";
import { fmtValue } from "../chart/axis";
import { GROUP_GUTTER_PX, PLOT_GUTTER_PX } from "./groupLink.svelte";

// plotKit's axisGutterSize budgets 7px per character for 10px sans labels
const CHAR_PX = 7, LABEL_PAD_PX = 4;

describe("plot y-axis gutters", () => {
  it("fit the widest heatmap / Q-Q tick labels (telemetry-nerd-es95: '2000 ms' must not clip)", () => {
    const widest = [`> ${fmtValue(2000, "ms")}`, `≤ ${fmtValue(2000, "ms")}`, fmtValue(2000, "ms")];
    for (const label of widest) {
      expect(label.length * CHAR_PX + LABEL_PAD_PX, label).toBeLessThanOrEqual(PLOT_GUTTER_PX);
    }
  });

  it("is no wider than the grouped gutter", () => {
    expect(PLOT_GUTTER_PX).toBeLessThanOrEqual(GROUP_GUTTER_PX);
  });
});
