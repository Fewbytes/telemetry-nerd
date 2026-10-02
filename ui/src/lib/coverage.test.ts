import { describe, expect, it } from "vitest";
import { windowBadge } from "./coverage";

const w = (columns: number, expected: number, unknown = false, unknown_columns?: number, missing_columns?: number) =>
  ({ label: "now", start_ms: 0, end_ms: 600_000, n: 10, columns, expected_columns: expected, unknown, unknown_columns, missing_columns }) as never;

describe("windowBadge", () => {
  it("is silent when the window is complete", () => expect(windowBadge(w(10, 10), 60_000)).toBeNull());
  it("states coverage and missing time", () => {
    expect(windowBadge(w(8, 10), 60_000)).toEqual({ text: "now: covers 80% · 2m missing", title: expect.stringContaining("2 of 10 steps") });
  });
  it("an empty expectation reads as no steps, not NaN or 0%", () => {
    const b = windowBadge(w(0, 0, true), 60_000);
    expect(b?.text).toBe("now: no steps to cover · part unknown");
    expect(`${b?.text}${b?.title}`).not.toMatch(/NaN|Infinity|0%/);
    expect(windowBadge(w(0, 0), 60_000)).toBeNull();
  });
  it("flags unknown data", () => expect(windowBadge(w(10, 10, true), 60_000)?.text).toContain("unknown"));
  it("unknown columns are not counted as missing", () => {
    const b = windowBadge(w(8, 10, true, 2, 0), 60_000);
    expect(b?.text).toBe("now: covers 80% · part unknown");
    expect(b?.title).toContain("0 of 10 steps");
    expect(windowBadge(w(7, 10, true, 2, 1), 60_000)?.text).toBe("now: covers 70% · 1m missing · part unknown");
  });
  it("an unknown column that holds data is not subtracted twice", () => {
    // expect 10, 8 returned (5 of them inside the failed chunk), 2 absent outside it
    expect(windowBadge(w(8, 10, true, 5, 2), 60_000)?.text).toBe("now: covers 80% · 2m missing · part unknown");
  });
  it("without unknown_columns (older payload) all absent columns read as missing", () => {
    expect(windowBadge(w(8, 10, true), 60_000)?.text).toBe("now: covers 80% · 2m missing · part unknown");
  });
});
