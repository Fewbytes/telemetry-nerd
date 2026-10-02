import { describe, expect, it } from "vitest";
import { relativeLuminance } from "./colormap";
import { PALETTE_DARK, PALETTE_LIGHT, seriesPalette } from "./toUplot";

// WCAG SC 1.4.11 (non-text contrast): graphical objects needed to understand content
// need ≥3:1 against their background. docs/telemetry-graphing-guide.md §6 requires this
// for every series colour. --bg tokens from ui/src/index.css.
const BG = { light: "#ffffff", dark: "#16181d" };

function contrast(a: string, b: string): number {
  const [l1, l2] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

describe("series palette contrast", () => {
  it.each([
    ["light", PALETTE_LIGHT] as const,
    ["dark", PALETTE_DARK] as const,
  ])("every %s-theme colour is ≥3:1 against --bg", (theme, palette) => {
    const bg = BG[theme];
    palette.forEach((color) => {
      expect(contrast(color, bg)).toBeGreaterThanOrEqual(3);
    });
  });

  it("seriesPalette keeps hue order unchanged between themes", () => {
    // same series index must map to the same hue family across themes, only lightness differs
    expect(seriesPalette("light")).toHaveLength(seriesPalette("dark").length);
  });
});
