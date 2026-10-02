import { afterEach, describe, expect, it, vi } from "vitest";
import { axisGutterSize } from "./plotKit";

// Minimal uPlot-like stub: just enough for axisGutterSize's `self.ctx.measureText` call.
// Width is modelled as 7 device px per character, like a typical 12px sans-serif digit.
function fakeSelf(dpr: number) {
  return {
    ctx: {
      save: () => {},
      restore: () => {},
      font: "",
      measureText: (s: string) => ({ width: s.length * 7 * dpr }),
    },
  } as unknown as Parameters<ReturnType<typeof axisGutterSize>>[0];
}

describe("axisGutterSize", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("returns the floor when there are no tick labels yet", () => {
    vi.stubGlobal("window", { devicePixelRatio: 1 });
    expect(axisGutterSize()(fakeSelf(1), [], 0, 0)).toBe(40);
  });

  it("widens the gutter to fit a long unit-scaled label (telemetry-nerd-klt: '1.4 GB/s' must not clip)", () => {
    vi.stubGlobal("window", { devicePixelRatio: 1 });
    const size = axisGutterSize()(fakeSelf(1), ["1.4 GB/s", "700 MB/s"], 0, 0);
    // 8 chars * 7px + 14px pad = 70, well past the 40px floor and past uPlot's old fixed 50px
    expect(size).toBe(70);
  });

  it("stays at the floor for short labels", () => {
    vi.stubGlobal("window", { devicePixelRatio: 1 });
    expect(axisGutterSize()(fakeSelf(1), ["0", "5", "10"], 0, 0)).toBe(40);
  });

  it("converts the device-pixel-scaled measurement back to CSS pixels", () => {
    vi.stubGlobal("window", { devicePixelRatio: 2 });
    const size = axisGutterSize()(fakeSelf(2), ["1.4 GB/s"], 0, 0);
    expect(size).toBe(70); // same CSS-pixel result regardless of dpr
  });
});
