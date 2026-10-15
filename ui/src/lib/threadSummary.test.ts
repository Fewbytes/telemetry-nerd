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
  it("exactly SNIPPET code points is not truncated", () =>
    expect(threadSummary(t([["u", "b".repeat(60)]]))).toBe(`1 message · u: "${"b".repeat(60)}"`));
  it("never splits an astral character at the boundary", () => {
    const out = threadSummary(t([["u", "c".repeat(59) + "😀" + "d".repeat(10)]]));
    expect(out).not.toMatch(/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/);
    expect(out).toBe(`1 message · u: "${"c".repeat(59)}😀…"`);
    expect([...out].join("")).toBe(out);
  });
});
