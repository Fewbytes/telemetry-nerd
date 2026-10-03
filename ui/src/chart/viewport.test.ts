import { describe, expect, it } from "vitest";
import { isInBounds, parseRelative, presetRange } from "./viewport";

describe("isInBounds", () => {
  it("true when the viewport is fully inside the fetched range", () => {
    const fetched = { start_ms: 0, end_ms: 10_000 };
    expect(isInBounds({ start_ms: 1000, end_ms: 9000 }, fetched)).toBe(true);
  });

  it("false when the viewport extends before the fetched start", () => {
    const fetched = { start_ms: 1000, end_ms: 10_000 };
    expect(isInBounds({ start_ms: 0, end_ms: 9000 }, fetched)).toBe(false);
  });

  it("false when the viewport extends past the fetched end", () => {
    const fetched = { start_ms: 0, end_ms: 9000 };
    expect(isInBounds({ start_ms: 1000, end_ms: 10_000 }, fetched)).toBe(false);
  });
});

describe("presetRange", () => {
  it("1h preset is exactly one hour ending now", () => {
    const now = 10_000_000;
    const v = presetRange("1h", now);
    expect(v.end_ms).toBe(now);
    expect(now - v.start_ms).toBe(3600_000);
  });
});

describe("parseRelative", () => {
  it("parses now-3h relative to now_ms", () => {
    const now = 10_000_000;
    expect(parseRelative("now-3h", now)).toEqual({ start_ms: now - 3 * 3600_000, end_ms: now });
  });

  it("returns null for unparseable text", () => {
    expect(parseRelative("not a range", 0)).toBeNull();
  });
});
