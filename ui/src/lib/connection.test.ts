import { describe, expect, it } from "vitest";
import type { Message, Presence, SessionPresence, Thread } from "./api";
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

describe("pill with per-session consumers (783)", () => {
  const withSessions = (
    status: Presence["status"],
    sessions: SessionPresence[],
  ): Presence => ({ kind: "presence", status, mode: null, since_ms: null, delivered_up_to: 0, sessions });

  const session = (consumer: string, s: SessionPresence["status"] = "live"): SessionPresence => ({
    consumer, kind: "claude", status: s, mode: "channel", since_ms: null,
  });

  it("shows the session id when exactly one session is live", () => {
    const p = withSessions("offline", [session("claude-a1b2c3")]);
    expect(pill("connected", p).label).toBe("Claude live · a1b2c3");
    expect(pill("connected", p).tone).toBe("ok");
  });

  it("counts instead of listing when several sessions are live", () => {
    const p = withSessions("offline", [session("claude-a"), session("claude-b")]);
    expect(pill("connected", p).label).toBe("2 Claude sessions live");
  });

  it("is live from the session list even when the UI consumer's frame is offline", () => {
    // the dtk-era inconsistency: claude-<sid> bridges bypass the UI consumer frame
    const p = withSessions("offline", [session("claude-a1b2c3")]);
    expect(pill("connected", p).command).toBeNull();
  });

  it("stays terminal-only when sessions exist but none is live", () => {
    const p = withSessions("offline", [session("claude-a", "terminal")]);
    expect(pill("connected", p).label).toBe("Terminal only");
    expect(pill("connected", p).command).toMatch(/^claude /);
  });

  it("falls back to the frame status for legacy daemons without sessions", () => {
    expect(pill("connected", presence("live")).label).toBe("Claude live");
    expect(pill("connected", presence("offline")).label).toBe("Claude offline");
  });
});
