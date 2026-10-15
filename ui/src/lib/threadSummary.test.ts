import { describe, expect, it } from "vitest";
import type { Thread } from "./api";
import { threadSummary } from "./threadSummary";

const t = (texts: [string, string][]): Thread =>
  ({ id: "t1", anchor: "p1", selection: null, author: "user", created_at_ms: 0,
    messages: texts.map(([author, text], i) => ({ id: `m${i}`, thread: "t1", author, text, created_at_ms: i, seq: null })) }) as Thread;

describe("threadSummary", () => {
  it("empty thread", () => expect(threadSummary(t([]))).toBe("No messages yet"));
  it("one message, singular, no ellipsis when short", () =>
    expect(threadSummary(t([["user", "why?"]]))).toBe('1 message · user: "why?"'));
  it("uses the last message and truncates at 60 chars", () => {
    const s = threadSummary(t([["user", "q"], ["claude", "a".repeat(100)]]));
    expect(s).toBe(`2 messages · claude: "${"a".repeat(60)}…"`);
  });
  it("collapses whitespace", () =>
    expect(threadSummary(t([["u", "a\n\n b"]]))).toBe('1 message · u: "a b"'));
});
