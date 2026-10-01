import { describe, expect, it } from "vitest";
import type { WorkspaceEvent } from "./api";
import { applyHighlightEvent, expire, nextExpiry, type Highlights } from "./highlights";

const ev = (
  type: string, object_id: string | null, actor: WorkspaceEvent["actor"] = "claude",
  payload: Record<string, unknown> = {},
): WorkspaceEvent => ({ seq: 1, ts_ms: 0, actor, type, object_id, klass: "internal", payload });
const hl = (id: string, note: string | null, ttl: number | null, actor: WorkspaceEvent["actor"] = "claude") =>
  ev("object.highlighted", id, actor, { note, ttl_ms: ttl });
const empty: Highlights = new Map();

describe("applyHighlightEvent", () => {
  it("adds a highlight with expiry = now + ttl", () => {
    const s = applyHighlightEvent(empty, hl("p3", "look", 1000), 500);
    expect(s.get("p3")).toEqual({ id: "p3", author: "claude", note: "look", expiresAt: 1500 });
  });
  it("null ttl means until cleared", () => {
    const s = applyHighlightEvent(empty, hl("p3", null, null, "user"), 500);
    expect(s.get("p3")).toMatchObject({ author: "user", expiresAt: null });
  });
  it("re-highlighting replaces the entry and resets the timer", () => {
    const a = applyHighlightEvent(empty, hl("p3", "one", 1000), 0);
    const b = applyHighlightEvent(a, hl("p3", "two", 1000), 900);
    expect(b.get("p3")).toMatchObject({ note: "two", expiresAt: 1900 });
    expect(a.get("p3")?.note).toBe("one"); // input not mutated
  });
  it("unhighlight removes", () => {
    const a = applyHighlightEvent(empty, hl("p3", null, null), 0);
    expect(applyHighlightEvent(a, ev("object.unhighlighted", "p3"), 0).size).toBe(0);
  });
  it("panel.closed clears that panel's highlight only", () => {
    let s = applyHighlightEvent(empty, hl("p3", null, null), 0);
    s = applyHighlightEvent(s, hl("f1", null, null), 0);
    s = applyHighlightEvent(s, ev("panel.closed", "p3"), 0);
    expect([...s.keys()]).toEqual(["f1"]);
  });
  it("returns the same state for irrelevant events", () => {
    const a = applyHighlightEvent(empty, hl("p3", null, null), 0);
    expect(applyHighlightEvent(a, ev("thread.message", "t1"), 0)).toBe(a);
    expect(applyHighlightEvent(a, ev("object.unhighlighted", "p9"), 0)).toBe(a);
  });
});

describe("expire / nextExpiry", () => {
  const s = applyHighlightEvent(
    applyHighlightEvent(empty, hl("p3", null, 1000), 0), hl("f1", null, null), 0,
  );
  it("drops entries whose time has passed, keeps pinned ones", () => {
    expect([...expire(s, 1000).keys()]).toEqual(["f1"]);
    expect(expire(s, 999)).toBe(s);
  });
  it("reports the earliest pending expiry", () => {
    expect(nextExpiry(s)).toBe(1000);
    expect(nextExpiry(expire(s, 1000))).toBeNull();
  });
});
