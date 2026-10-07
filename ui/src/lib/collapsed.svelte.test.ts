import { describe, expect, it } from "vitest";
import { CollapsedStore } from "./collapsed.svelte";

// No `window` in this vitest environment (node), so the store starts empty and persistence is a
// no-op — this exercises the in-memory half; collapsed.svelte.ts's try/catch covers the rest.
describe("CollapsedStore", () => {
  it("starts with nothing collapsed", () => {
    const s = new CollapsedStore();
    expect(s.has("p1")).toBe(false);
  });

  it("toggle flips membership", () => {
    const s = new CollapsedStore();
    s.toggle("p1");
    expect(s.has("p1")).toBe(true);
    s.toggle("p1");
    expect(s.has("p1")).toBe(false);
  });

  it("tracks multiple panels independently", () => {
    const s = new CollapsedStore();
    s.toggle("p1");
    s.toggle("p2");
    expect(s.has("p1")).toBe(true);
    expect(s.has("p2")).toBe(true);
    s.toggle("p1");
    expect(s.has("p1")).toBe(false);
    expect(s.has("p2")).toBe(true);
  });
});
