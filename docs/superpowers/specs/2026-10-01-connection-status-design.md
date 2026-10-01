# Connection status and reliable channel delivery

Date: 2026-10-01 · Beads: telemetry-nerd-jxp (bridge drops events before session), connection
status feature. Follow-ups: k01 (multi-user/session), xgx (remote deploy skill), r2v
(busy/idle via hooks), grd (bridge startup with daemon down).

## Problem

The UI exists to talk to Claude, but "no live Claude" is a normal state: no session running,
or a session without `--channels` (hook delivery happens only on the next terminal prompt).
The UI shows none of this, so questions look sent but go nowhere. Separately, the bridge
heartbeats before its MCP session exists, which suppresses the hook while the pump claims and
drops events (jxp).

Scope: local and remote-daemon setups (Claude Code on a remote box is the same as a remote
daemon), one user per workspace, one active Claude session.

## §1 Daemon presence

- `GET /ws/bridge?consumer=claude`: one socket per bridge for its lifetime, in every mode.
  Control/delivery frames only, no raw event stream.
  - bridge → daemon: `{"type":"hello","mode":"hook"|"channel"}`, `{"type":"mode","mode":"channel"}`,
    `{"type":"ready"}`, `{"type":"ack","up_to":N}`
  - daemon → bridge: `{"type":"deliver","up_to":N,"content":...,"meta":{...}}`
- `PresenceRegistry` (`core/presence.py`, in memory): connected bridges
  `{conn_id, consumer, mode, ready, since_ms}`; status per consumer:
  `live` (some bridge with mode=channel and ready) · `terminal` (connected, not live) ·
  `offline` (none).
- Hook suppression: `/api/channel/status` reports `channel_active = live`. `/api/channel/claim`
  returns nothing while live (server-side check; no status-then-claim race).
- Heartbeat endpoint, heartbeat loop and `heartbeat_ms` use are removed.
- Liveness: WS close (immediate locally); uvicorn and `websockets` pings detect dead WAN links
  (~40 s). Close unregisters and recomputes status.
- UI `/ws` gets `{"kind":"presence","status","mode","since_ms","delivered_up_to"}` on connect
  and on every change (registry change, ack, hook claim). Control frame, no `seq`, not logged.
- Daemon restart: empty registry; bridges reconnect and resend hello/ready.

## §2 Delivery (daemon-driven, message + ack)

- While a consumer is live and intentional events exist past its cursor, the daemon formats the
  batch (`format_channel`, same as the hook) and sends `deliver` to the live bridge (the
  earliest-connected live one). One delivery in flight per consumer.
- Bridge calls `send_notification`, then sends `ack{up_to}`; the ack advances the cursor
  (monotonic). No ack (send failed, socket dropped) → cursor unchanged → redelivered after the
  next `ready`. At-least-once; `seqs` in meta exposes duplicates.
- Triggers: `ready`, `ack` (more may be pending), new intentional event appended.
- Bridge pump: connect with backoff, hello/mode/ready, on deliver notify→ack. No HTTP claim, no
  event filtering, no heartbeat. `ready` is sent once the MCP session is observed, so a channel
  bridge never receives events it cannot send (fixes jxp). `notify` without a session raises.
- Hook path unchanged: HTTP claim advances the cursor immediately; refused while live.

## §3 UI

- `connection` state: `daemon: connected|reconnecting` (UI `/ws` open/close) and `presence`
  (latest frame; unknown while reconnecting). `subscribe()` routes `kind:"presence"` frames
  separately.
- Header pill next to the theme toggle, `button` with `aria-live="polite"`; label text carries
  the state (never color alone):
  - `Daemon unreachable` — reconnecting, shows daemon URL
  - `Claude live`
  - `Terminal only` — "Claude sees UI questions on your next message in its terminal; start with
    `--channels` for live delivery"
  - `Claude offline` — copyable start command; questions are kept and delivered on connect
- User thread messages show `queued` (seq > delivered_up_to) → `delivered` → `answered` (a
  Claude message follows in the thread).
- Composer stays enabled; placeholder reflects state.

## §4 Tests

- Unit: registry derivation and removal; event log ack/peek; daemon delivery (only while live,
  one in flight, ack advances, unacked redelivered on ready, claim refused while live); pump
  hello/mode/ready ordering, resend on reconnect, notify→ack, failed notify → no ack; hook
  suppression from registry.
- Integration (stdio bridge e2e): question posted before the MCP handshake arrives exactly once
  (jxp); link drop before ack redelivers; presence transitions offline→terminal→live→offline
  observed on UI `/ws`.
- UI: vitest for presence routing and daemon connection state; Playwright for pill labels,
  daemon unreachable/recovery, message status queued→delivered→answered.

## Out of scope

SSE instead of WS; multi-session routing (k01); busy/idle (r2v); bridge startup without daemon
(grd); embedded Agent SDK answerer (spec §7.3).
