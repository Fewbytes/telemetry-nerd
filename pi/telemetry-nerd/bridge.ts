// Daemon side of the pi extension: the `/ws/bridge` channel (what ChannelPump does for Claude
// Code, src/telemetry_nerd/bridge/channel.py) and the hook-mode claim (what `telemetry-nerd
// pending` does). Kept free of the pi API so tests drive it with a fake socket.

export type EventMeta = Record<string, string>;

/** The slice of a WebSocket the bridge uses; the global WebSocket satisfies it. */
export interface BridgeSocket {
  send(data: string): void;
  close(): void;
  onopen: (() => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: (() => void) | null;
}

export interface ChannelBridgeOptions {
  daemonUrl: string;
  /** Session-local consumer id (`pi-<session>`), so concurrent sessions keep separate cursors. */
  consumer: string;
  connect: (url: string) => BridgeSocket;
  /** Puts the events into the session; the delivery is acked only once this resolves. */
  inject: (content: string, meta: EventMeta) => Promise<void>;
  /** Runs `reconnect` after `delayMs`; injected so tests need no real timers. */
  schedule: (reconnect: () => void, delayMs: number) => void;
}

export const BACKOFF_MS = 1000;
export const MAX_BACKOFF_MS = 30_000;

export function bridgeUrl(daemonUrl: string, consumer: string): string {
  const url = new URL("/ws/bridge", daemonUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("consumer", consumer);
  return url.toString();
}

/** Holds the bridge socket: reports `channel` mode and readiness, injects and acks deliveries. */
export class ChannelBridge {
  private backoffMs = BACKOFF_MS;
  private socket: BridgeSocket | null = null;
  private stopped = false;
  private connectedNow = false;

  constructor(private readonly options: ChannelBridgeOptions) {}

  /** True while the socket is open and readiness was reported: the daemon refuses hook claims. */
  get connected(): boolean {
    return this.connectedNow;
  }

  start(): void {
    this.stopped = false;
    const socket = this.options.connect(bridgeUrl(this.options.daemonUrl, this.options.consumer));
    this.socket = socket;
    socket.onopen = () => {
      this.backoffMs = BACKOFF_MS;
      socket.send(JSON.stringify({ type: "hello", mode: "channel" }));
      socket.send(JSON.stringify({ type: "ready" }));
      this.connectedNow = true;
    };
    socket.onmessage = (event) => void this.onFrame(socket, String(event.data));
    socket.onclose = () => {
      this.connectedNow = false;
      if (this.stopped || this.socket !== socket) return;
      const delay = this.backoffMs;
      this.backoffMs = Math.min(delay * 2, MAX_BACKOFF_MS);
      this.options.schedule(() => this.start(), delay);
    };
  }

  stop(): void {
    this.stopped = true;
    this.connectedNow = false;
    this.socket?.close();
    this.socket = null;
  }

  private async onFrame(socket: BridgeSocket, text: string): Promise<void> {
    const frame = JSON.parse(text) as {
      type: string;
      content: string;
      meta?: EventMeta;
      up_to: number;
    };
    if (frame.type !== "deliver") {
      console.warn(`telemetry-nerd: ignoring unknown bridge frame ${frame.type}`);
      return;
    }
    try {
      await this.options.inject(frame.content, frame.meta ?? {});
    } catch (e) {
      // Unacked: dropping the socket makes the daemon requeue the delivery for the next connect.
      console.error(`telemetry-nerd: inject failed (${String(e)}); reconnecting to redeliver`);
      socket.close();
      return;
    }
    socket.send(JSON.stringify({ type: "ack", up_to: frame.up_to }));
  }
}

/** Wraps claimed events the way `telemetry-nerd pending` prints them. */
export function formatClaimed(content: string, seqs: number[]): string {
  return `<telemetry-nerd-ui-events seqs="${seqs.join(",")}">\n${content}\n</telemetry-nerd-ui-events>`;
}

/**
 * Hook mode: claims this consumer's pending events. Null when there is nothing, or when a live
 * channel bridge of this kind delivers them instead (the daemon refuses the claim). Throws on a
 * non-2xx reply so a broken daemon is not mistaken for "no events".
 */
export async function claimPending(
  daemonUrl: string,
  consumer: string,
  fetchFn: typeof fetch = fetch,
): Promise<string | null> {
  const response = await fetchFn(new URL("/api/channel/claim", daemonUrl), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ consumer }),
  });
  if (!response.ok) throw new Error(`claim failed: HTTP ${response.status}`);
  const data = (await response.json()) as { content: string | null; seqs?: number[] };
  return data.content ? formatClaimed(data.content, data.seqs ?? []) : null;
}
