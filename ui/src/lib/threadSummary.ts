import type { Thread } from "./api";

const SNIPPET = 60;

/** One-line summary of a thread for simple mode: message count and the last message's start. */
export function threadSummary(thread: Thread): string {
  const n = thread.messages.length;
  if (n === 0) return "No messages yet";
  const last = thread.messages[n - 1];
  const text = last.text.replace(/\s+/g, " ").trim();
  const snippet = text.length > SNIPPET ? `${text.slice(0, SNIPPET)}…` : text;
  return `${n} message${n === 1 ? "" : "s"} · ${last.author}: "${snippet}"`;
}
