import type { Presence, Thread } from "./api";

export type DaemonState = "connecting" | "connected" | "reconnecting";
export type Tone = "ok" | "warn" | "off" | "error" | "neutral";
export type MessageStatus = "queued" | "delivered" | "answered";

/** Development-channel start command (channels are a research preview). */
export const START_COMMAND = "claude --dangerously-load-development-channels server:telemetry-nerd";

export interface Pill {
  label: string;
  tone: Tone;
  detail: string;
  command: string | null;
}

/** What the header pill shows for the daemon socket state and the latest presence frame. */
function withSessions(detail: string, presence: Presence): string {
  const live = (presence.sessions ?? []).filter((s) => s.status === "live");
  if (live.length === 0) return detail;
  const list = live.map((s) => `${s.consumer} (${s.mode ?? "?"})`).join(", ");
  return `${detail}\nConnected sessions: ${list}`;
}

/** The session part of a per-session consumer id (claude-a1b2 → a1b2); plain ids pass through. */
function shortSession(consumer: string): string {
  return consumer.includes("-") ? consumer.slice(consumer.indexOf("-") + 1) : consumer;
}

/**
 * Effective presence status (783): per-session bridges register as claude-<sid>, so the
 * UI consumer's own frame can be offline while sessions are live — derive from the
 * session list when it is present, falling back to the frame for legacy daemons.
 */
function effectiveStatus(presence: Presence): Presence["status"] {
  const sessions = presence.sessions ?? [];
  if (sessions.length === 0) return presence.status;
  if (sessions.some((s) => s.status === "live")) return "live";
  return "terminal";
}

function sessionLabel(presence: Presence): string | null {
  const live = (presence.sessions ?? []).filter((s) => s.status === "live");
  if (live.length === 0) return null;
  if (live.length === 1) return `Claude live · ${shortSession(live[0].consumer)}`;
  return `${live.length} Claude sessions live`;
}

export function pill(daemon: DaemonState, presence: Presence | null): Pill {
  if (daemon === "reconnecting") {
    return {
      label: "Daemon unreachable", tone: "error", command: null,
      detail: "Lost the connection to the Telemetry Nerd daemon; reconnecting…",
    };
  }
  if (presence === null) {
    return { label: "Connecting…", tone: "neutral", detail: "Waiting for the daemon.", command: null };
  }
  switch (effectiveStatus(presence)) {
    case "live":
      return {
        label: sessionLabel(presence) ?? "Claude live", tone: "ok", command: null,
        detail: withSessions("Questions reach Claude as soon as you send them.", presence),
      };
    case "terminal":
      return {
        label: "Terminal only", tone: "warn", command: START_COMMAND,
        detail: withSessions(
          "Claude sees UI questions when you next send a message in its terminal. " +
            "Start Claude with the channel enabled for live delivery:",
          presence,
        ),
      };
    case "offline":
      return {
        label: "Claude offline", tone: "off", command: START_COMMAND,
        detail: withSessions(
          "No Claude session is connected. Questions are kept and delivered when one starts:",
          presence,
        ),
      };
  }
}

/** Delivery state of a user's message: null for Claude's messages or before presence is known. */
export function messageStatus(
  thread: Thread, index: number, presence: Presence | null,
): MessageStatus | null {
  const m = thread.messages[index];
  if (m.author !== "user") return null;
  if (thread.messages.slice(index + 1).some((r) => r.author === "claude")) return "answered";
  if (presence === null || m.seq === null) return null;
  return m.seq <= presence.delivered_up_to ? "delivered" : "queued";
}

/** Composer placeholder suffix telling the user where a message will go. */
export function composeHint(presence: Presence | null): string {
  switch (presence?.status) {
    case "terminal":
      return " — Claude sees it on your next terminal message";
    case "offline":
      return " — Claude offline, delivered when it connects";
    default:
      return "";
  }
}
