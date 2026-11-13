// pi extension for telemetry-nerd: user workspace events reach the pi session over the daemon's
// `/ws/bridge` channel; before_agent_start claims them when the channel is not up (hook mode).
// The agent answers through the existing MCP `reply` tool; the daemon is unchanged.
//
// Busy/idle is tracked here only, to pick how a delivery enters the session. The daemon has no
// busy frame yet (telemetry-nerd-r2v), so the UI cannot show it.
import { ChannelBridge, claimPending } from "./bridge.ts";

const CUSTOM_TYPE = "telemetry-nerd-ui-events";
const ENSURE_URL_PREFIX = "Telemetry Nerd workspace: ";

// Structural slice of pi's ExtensionAPI that this extension uses.
interface PiSessionContext {
  sessionManager: { getSessionId(): string };
}
interface PiApi {
  on(event: "session_start", handler: (event: unknown, ctx: PiSessionContext) => Promise<void>): void;
  on(event: "session_shutdown", handler: () => Promise<void>): void;
  on(event: "agent_start" | "agent_end", handler: () => Promise<void>): void;
  on(
    event: "before_agent_start",
    handler: () => Promise<{ message: { customType: string; content: string; display: boolean } } | undefined>,
  ): void;
  exec(command: string, args: string[]): Promise<{ stdout: string; code: number }>;
  sendMessage(
    message: { customType: string; content: string; display: boolean; details?: unknown },
    options: { triggerTurn?: boolean; deliverAs?: "steer" | "followUp" | "nextTurn" },
  ): void | Promise<void>;
}

export default function telemetryNerd(pi: PiApi): void {
  let daemonUrl: string | null = null;
  let consumer = "";
  let bridge: ChannelBridge | null = null;
  let busy = false;

  pi.on("session_start", async (_event, ctx) => {
    consumer = process.env.TN_CONSUMER ?? `pi-${ctx.sessionManager.getSessionId()}`;
    const ensured = await pi.exec("telemetry-nerd", ["ensure"]);
    const line = ensured.stdout.split("\n").find((l) => l.startsWith(ENSURE_URL_PREFIX));
    if (ensured.code !== 0 || !line) {
      console.error(`telemetry-nerd: daemon unavailable: ${ensured.stdout.trim()}`);
      return;
    }
    daemonUrl = line.slice(ENSURE_URL_PREFIX.length).trim();
    bridge = new ChannelBridge({
      daemonUrl,
      consumer,
      connect: (url) => new WebSocket(url),
      inject: async (content, meta) =>
        await pi.sendMessage(
          { customType: CUSTOM_TYPE, content, display: true, details: meta },
          busy ? { deliverAs: "steer" } : { triggerTurn: true },
        ),
      schedule: (reconnect, delayMs) => void setTimeout(reconnect, delayMs),
    });
    bridge.start();
  });

  pi.on("session_shutdown", async () => bridge?.stop());
  pi.on("agent_start", async () => void (busy = true));
  pi.on("agent_end", async () => void (busy = false));

  pi.on("before_agent_start", async () => {
    if (daemonUrl === null || bridge?.connected) return undefined;
    const content = await claimPending(daemonUrl, consumer);
    return content ? { message: { customType: CUSTOM_TYPE, content, display: true } } : undefined;
  });
}
