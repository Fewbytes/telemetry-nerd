import { afterEach, describe, expect, it, vi } from "vitest";
import { subscribe, type WorkspaceEvent } from "./api";
import { needsReload } from "./workspace.svelte";

const ev = (type: string, klass: WorkspaceEvent["klass"]): WorkspaceEvent => ({
  seq: 1, ts_ms: 0, actor: "claude", type, object_id: null, klass, payload: {},
});

describe("needsReload", () => {
  it("reloads for any non-internal event", () => {
    expect(needsReload(ev("whatever", "ambient"))).toBe(true);
    expect(needsReload(ev("whatever", "intentional"))).toBe(true);
  });
  it("ignores unknown internal events", () => {
    expect(needsReload(ev("cache.warm", "internal"))).toBe(false);
  });
  it("reloads for listed types even when internal", () => {
    for (const t of ["panel.created", "panel.answered", "finding.created", "finding.verdict",
      "annotation.created", "annotation.deleted", "hypothesis.created",
      "hypothesis.status_changed", "gap.created", "thread.message", "panel.closed"]) {
      expect(needsReload(ev(t, "internal"))).toBe(true);
    }
  });
});

describe("subscribe", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("builds /ws?since=<lastSeq> and re-reads since on reconnect", () => {
    const urls: string[] = [];
    const sockets: FakeWS[] = [];
    class FakeWS {
      onmessage: ((m: { data: string }) => void) | null = null;
      onclose: (() => void) | null = null;
      constructor(url: string) {
        urls.push(url);
        sockets.push(this);
      }
      close() {}
    }
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeWS);
    vi.stubGlobal("location", { protocol: "http:", host: "h:1" });
    let seq = 12;
    const stop = subscribe(() => {}, () => seq);
    expect(urls[0]).toBe("ws://h:1/ws?since=12");
    seq = 20;
    // reconnect path: onclose schedules a new connect() which re-reads sinceRef
    sockets[0].onclose!();
    vi.advanceTimersByTime(1000);
    expect(urls[1]).toBe("ws://h:1/ws?since=20");
    stop();
    vi.useRealTimers();
  });
});
