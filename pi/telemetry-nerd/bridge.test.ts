import { describe, expect, it } from "vitest";
import {
  BACKOFF_MS,
  ChannelBridge,
  claimPending,
  type BridgeSocket,
  type EventMeta,
} from "./bridge.ts";

class FakeSocket implements BridgeSocket {
  sent: unknown[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(readonly url: string) {}
  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }
  close(): void {
    this.closed = true;
    this.onclose?.();
  }
  /** Deliver a daemon frame and wait until the bridge's async handler has settled. */
  async receive(frame: object): Promise<void> {
    this.onmessage?.({ data: JSON.stringify(frame) });
    await Promise.resolve();
    await Promise.resolve();
  }
}

function arrange(inject: (content: string, meta: EventMeta) => Promise<void>) {
  const sockets: FakeSocket[] = [];
  const reconnects: { run: () => void; delayMs: number }[] = [];
  const bridge = new ChannelBridge({
    daemonUrl: "http://127.0.0.1:7070",
    consumer: "pi-s1",
    connect: (url) => sockets.push(new FakeSocket(url)) && sockets[sockets.length - 1]!,
    inject,
    schedule: (run, delayMs) => reconnects.push({ run, delayMs }),
  });
  return { bridge, sockets, reconnects };
}

describe("ChannelBridge", () => {
  it("connects as its own session consumer and reports channel mode then readiness", () => {
    const { bridge, sockets } = arrange(async () => {});
    bridge.start();
    sockets[0]!.onopen?.();
    expect(sockets[0]!.url).toBe("ws://127.0.0.1:7070/ws/bridge?consumer=pi-s1");
    expect(sockets[0]!.sent).toEqual([{ type: "hello", mode: "channel" }, { type: "ready" }]);
    expect(bridge.connected).toBe(true);
  });

  it("acks a delivery only after the events were injected", async () => {
    const injected: [string, EventMeta][] = [];
    const { bridge, sockets } = arrange(async (content, meta) => void injected.push([content, meta]));
    bridge.start();
    sockets[0]!.onopen?.();
    await sockets[0]!.receive({ type: "deliver", up_to: 3, content: "user asked why", meta: { thread: "t1" } });
    expect(injected).toEqual([["user asked why", { thread: "t1" }]]);
    expect(sockets[0]!.sent.at(-1)).toEqual({ type: "ack", up_to: 3 });
  });

  it("drops the socket without acking when injection fails, so the daemon redelivers", async () => {
    const { bridge, sockets, reconnects } = arrange(async () => {
      throw new Error("session gone");
    });
    bridge.start();
    sockets[0]!.onopen?.();
    await sockets[0]!.receive({ type: "deliver", up_to: 3, content: "x" });
    expect(sockets[0]!.sent.some((f) => (f as { type: string }).type === "ack")).toBe(false);
    expect(sockets[0]!.closed).toBe(true);
    expect(reconnects).toHaveLength(1);
  });

  it("reconnects with doubling backoff and reports again after reopening", () => {
    const { bridge, sockets, reconnects } = arrange(async () => {});
    bridge.start();
    sockets[0]!.close();
    expect(bridge.connected).toBe(false);
    expect(reconnects[0]!.delayMs).toBe(BACKOFF_MS);
    reconnects[0]!.run();
    sockets[1]!.close();
    expect(reconnects[1]!.delayMs).toBe(BACKOFF_MS * 2);
    reconnects[1]!.run();
    sockets[2]!.onopen?.();
    expect(sockets[2]!.sent).toEqual([{ type: "hello", mode: "channel" }, { type: "ready" }]);
  });

  it("does not reconnect after stop", () => {
    const { bridge, sockets, reconnects } = arrange(async () => {});
    bridge.start();
    bridge.stop();
    expect(sockets[0]!.closed).toBe(true);
    expect(reconnects).toHaveLength(0);
  });
});

describe("claimPending", () => {
  const reply = (body: object, status = 200) => async () => new Response(JSON.stringify(body), { status });

  it("wraps claimed events like the pending hook and posts this session's consumer", async () => {
    let posted: unknown;
    const fetchFn = (async (_url: URL, init: RequestInit) => {
      posted = JSON.parse(String(init.body));
      return new Response(JSON.stringify({ content: "why?", seqs: [4, 5] }));
    }) as unknown as typeof fetch;
    const text = await claimPending("http://127.0.0.1:7070", "pi-s1", fetchFn);
    expect(posted).toEqual({ consumer: "pi-s1" });
    expect(text).toBe('<telemetry-nerd-ui-events seqs="4,5">\nwhy?\n</telemetry-nerd-ui-events>');
  });

  it("returns null when nothing is pending or a live channel delivers instead", async () => {
    const none = reply({ content: null, live: true }) as unknown as typeof fetch;
    expect(await claimPending("http://127.0.0.1:7070", "pi-s1", none)).toBeNull();
  });

  it("throws on a daemon error rather than reporting no events", async () => {
    const broken = reply({}, 500) as unknown as typeof fetch;
    await expect(claimPending("http://127.0.0.1:7070", "pi-s1", broken)).rejects.toThrow("HTTP 500");
  });
});
