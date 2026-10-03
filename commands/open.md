---
description: Show the Telemetry Nerd workspace UI URL
allowed-tools: Bash(sh:*), Bash(open:*), Bash(xdg-open:*), mcp__plugin_telemetry-nerd_telemetry-nerd__workspace_get
---

Open the workspace UI.

1. Find the URL: the SessionStart hook context line, else the URL in the MCP server
   instructions (default http://127.0.0.1:7070), else run
   `sh "${CLAUDE_PLUGIN_ROOT}/scripts/tn-launch" ensure` (it starts the daemon if needed and
   prints the URL). If that fails, relay its message and stop.
2. Print the URL. Add a one-line brief from `workspace_get`: panel, hypothesis and finding counts and
   any open threads. When the workspace has specific objects the user is working on, link them as
   `<url>/#/panel/<id>`, `/#/finding/<id>`.
3. Try to open it in the browser (`open <url>` on macOS, `xdg-open <url>` on Linux); if that is
   unavailable, just print the URL. Do not retry.
