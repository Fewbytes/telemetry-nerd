import { describe, expect, it } from "vitest";
import { isInBounds, nowAnchor, parseRelative, presetRange } from "./viewport";

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

  it("a viewport a few ms past the fetched end is in bounds with tolerance", () => {
    const fetched = { start_ms: 0, end_ms: 9000 };
    expect(isInBounds({ start_ms: 1000, end_ms: 9003 }, fetched, 5)).toBe(true);
    expect(isInBounds({ start_ms: 1000, end_ms: 9010 }, fetched, 5)).toBe(false);
  });

  it("regression: a now-anchored preset computed with Date.now() was effectively unreachable", () => {
    // this is exactly the bug: `fetched.end_ms` is frozen at fetch time, `Date.now()` keeps
    // moving, so a preset's end (resolved against the wall clock a moment later) is always a
    // few ms past it and isInBounds never fires without either nowAnchor or tolerance.
    const fetched = { start_ms: 0, end_ms: 10_000_000 };
    const presetEndResolvedLater = fetched.end_ms + 37; // a moment passed before the click landed
    expect(isInBounds({ start_ms: 9_000_000, end_ms: presetEndResolvedLater }, fetched)).toBe(
      false,
    );
  });
});

describe("nowAnchor", () => {
  it("anchors to the fetched end when there is one, not the wall clock", () => {
    expect(nowAnchor(10_000_000, 10_000_037)).toBe(10_000_000);
  });

  it("falls back to the wall clock before anything has been fetched", () => {
    expect(nowAnchor(null, 10_000_037)).toBe(10_000_037);
    expect(nowAnchor(undefined, 10_000_037)).toBe(10_000_037);
    expect(nowAnchor(0, 10_000_037)).toBe(10_000_037);
  });

  it("makes a preset no wider than the fetched span land in bounds", () => {
    const fetched = { start_ms: 0, end_ms: 10_000_000 };
    const clockNowMs = fetched.end_ms + 37; // time passed between fetch and click
    const v = presetRange("1h", nowAnchor(fetched.end_ms, clockNowMs));
    expect(isInBounds(v, fetched)).toBe(true);
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
