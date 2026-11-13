# telemetry-nerd pi extension

Install as a pi package from this directory (`pi install ./pi/telemetry-nerd`). It needs
`telemetry-nerd` on PATH (or `TN_DAEMON_URL` pointing at a running daemon) and the telemetry-nerd MCP
server configured for the agent, whose `reply` tool answers user threads.

- `session_start`: runs `telemetry-nerd ensure`, then holds `/ws/bridge?consumer=pi-<session id>`.
  Delivered events enter the session as a `telemetry-nerd-ui-events` message (steering a running
  turn, or starting one when idle).
- `before_agent_start`: while the socket is not up, claims pending events over
  `/api/channel/claim` (hook mode) and adds them to the run.
- Consumers are `pi-<session id>` (override: `TN_CONSUMER`), so a pi and a Claude Code session on one
  workspace keep separate cursors and never take each other's events.
