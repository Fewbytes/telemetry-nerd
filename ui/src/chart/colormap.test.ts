import { describe, expect, it } from "vitest";
import { colormap, relativeLuminance } from "./colormap";

describe("colormap", () => {
  it("has the published endpoints", () => {
    expect(colormap("viridis")(0)).toBe("rgb(68,1,84)");
    expect(colormap("viridis")(1)).toBe("rgb(253,231,37)");
    expect(colormap("cividis")(0)).toBe("rgb(0,34,78)");
  });
  it.each(["viridis", "cividis"] as const)("%s is monotonic in luminance (perceptually ordered)", (name) => {
    const f = colormap(name);
    const l = Array.from({ length: 65 }, (_, i) => relativeLuminance(f(i / 64)));
    l.slice(1).forEach((v, i) => expect(v).toBeGreaterThan(l[i]));
  });
  it("clamps out-of-range and non-finite input", () => {
    const f = colormap("viridis");
    expect(f(-1)).toBe(f(0));
    expect(f(2)).toBe(f(1));
    expect(f(Number.NaN)).toBe(f(0));
  });
});
