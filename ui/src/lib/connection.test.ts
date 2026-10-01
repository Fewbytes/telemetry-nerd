import { describe, expect, it } from "vitest";
import type { Message, Presence, Thread } from "./api";
import { messageStatus, pill } from "./connection";

const presence = (status: Presence["status"], delivered_up_to = 0): Presence => ({
  kind: "presence", status, mode: status === "live" ? "channel" : null,
  since_ms: null, delivered_up_to,
});

const msg = (author: string, seq: number | null, id = `m${seq}`): Message => ({
  id, thread: "t1", author, text: "x", created_at_ms: 0, seq,
});

const thread = (...messages: Message[]): Thread => ({
  id: "t1", anchor: null, selection: null, author: "user", created_at_ms: 0, messages,
});

describe("pill", () => {
  it("daemon down wins over any stale presence", () => {
    expect(pill("reconnecting", presence("live")).label).toBe("Daemon unreachable");
  });
  it("labels each presence status", () => {
    expect(pill("connected", presence("live")).label).toBe("Claude live");
    expect(pill("connected", presence("terminal")).label).toBe("Terminal only");
    expect(pill("connected", presence("offline")).label).toBe("Claude offline");
  });
  it("is neutral before the first presence frame", () => {
    expect(pill("connecting", null).label).toBe("Connecting…");
    expect(pill("connected", null).label).toBe("Connecting…");
  });
  it("offers a start command only when offline or terminal-only", () => {
    expect(pill("connected", presence("offline")).command).toMatch(/^claude /);
    expect(pill("connected", presence("terminal")).command).toMatch(/^claude /);
    expect(pill("connected", presence("live")).command).toBeNull();
  });
});

describe("messageStatus", () => {
  it("is queued until the cursor passes the message", () => {
    const t = thread(msg("user", 5));
    expect(messageStatus(t, 0, presence("offline", 4))).toBe("queued");
    expect(messageStatus(t, 0, presence("live", 5))).toBe("delivered");
  });
  it("is answered once Claude posts after it", () => {
    const t = thread(msg("user", 5), msg("claude", 6));
    expect(messageStatus(t, 0, presence("live", 4))).toBe("answered");
  });
  it("a later question is not answered by an earlier reply", () => {
    const t = thread(msg("user", 1), msg("claude", 2), msg("user", 3));
    expect(messageStatus(t, 2, presence("live", 2))).toBe("queued");
  });
  it("has no status for Claude's messages or without presence", () => {
    const t = thread(msg("claude", 2), msg("user", 3));
    expect(messageStatus(t, 0, presence("live", 9))).toBeNull();
    expect(messageStatus(t, 1, null)).toBeNull();
  });
});
