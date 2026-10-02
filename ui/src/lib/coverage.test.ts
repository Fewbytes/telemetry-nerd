import { describe, expect, it } from "vitest";
import { windowBadge } from "./coverage";

const w = (columns: number, expected: number, unknown = false) =>
  ({ label: "now", start_ms: 0, end_ms: 600_000, n: 10, columns, expected_columns: expected, unknown }) as never;

describe("windowBadge", () => {
  it("is silent when the window is complete", () => expect(windowBadge(w(10, 10), 60_000)).toBeNull());
  it("states coverage and missing time", () => {
    expect(windowBadge(w(8, 10), 60_000)).toEqual({ text: "now: covers 80% · 2m missing", title: expect.stringContaining("2 of 10 steps") });
  });
  it("flags unknown data", () => expect(windowBadge(w(10, 10, true), 60_000)?.text).toContain("unknown"));
});
