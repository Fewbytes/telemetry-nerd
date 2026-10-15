import { describe, expect, it } from "vitest";
import { heatOutlineText, heatPercentileHint, spectrogramLegend, spectrumLegend } from "./captions";

describe("captions (one string per dense legend, so simple mode can show it as one line)", () => {
  it("spectrum: method, step, the two levels, red-noise caveat and peak count", () => {
    const t = spectrumLegend(60_000, 1, false);
    expect(t).toBe(
      "Lomb-Scargle, linear trend removed · step 60s · dashed: 1% false-alarm level (white noise) · dotted: 1% level against AR(1) red noise (significant = above both) · 1 significant peak",
    );
    expect(spectrumLegend(30_000, 3, true)).toContain("(significant = above both); long periods overstated by white noise (autocorrelation) · 3 significant peaks");
  });

  it("spectrogram: window, hop, resolution, period range and colour key", () => {
    expect(spectrogramLegend({ segment_ms: 960_000, hop_ms: 480_000, overlap: 0.5, limits: { shortest_s: 120.4, longest_s: 479.6 } })).toBe(
      "window 16m · hop 8m (50% overlap): each column summarizes one window · period resolution 1/16m, rows show the max within the row · periods 120s–480s · colour: share of window variance, linear 0–1 · faint: below the 1% false-alarm level · hatched: window under 50% covered",
    );
  });

  it("heatmap percentile hint names the column and the n rule", () => {
    expect(heatPercentileHint(60_000)).toBe("bucket holding q per 1m column, only where n ≥ 10/(1−q); faded lane: too few observations");
  });

  it("heatmap outline names the quantile and its n floor", () => {
    expect(heatOutlineText(0.95)).toBe("outline: bucket holding p95 (only where n ≥ 200)");
    expect(heatOutlineText(0.5)).toBe("outline: bucket holding p50 (only where n ≥ 20)");
    expect(heatOutlineText(0.99)).toBe("outline: bucket holding p99 (only where n ≥ 1000)");
  });
});
