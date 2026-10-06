import { describe, expect, it } from "vitest";
import { panelLoadingState } from "./loading";

describe("panelLoadingState", () => {
  it("is idle when not loading, with or without data", () => {
    expect(panelLoadingState(false, false)).toBe(null);
    expect(panelLoadingState(false, true)).toBe(null);
  });

  it("shows a skeleton on first load (nothing to show yet)", () => {
    expect(panelLoadingState(true, false)).toBe("skeleton");
  });

  it("shows an overlay on refetch (stale data still on screen)", () => {
    expect(panelLoadingState(true, true)).toBe("overlay");
  });
});
