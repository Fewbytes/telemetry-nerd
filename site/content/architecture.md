---
title: "Architecture"
description: "How the Claude Code plugin, the MCP bridge and the daemon fit together, and how to run Telemetry Nerd locally or on a remote box."
---

Telemetry Nerd is three pieces talking to each other: a Claude Code **plugin** (skills, commands
and hooks, running inside your Claude Code session), a small **MCP bridge** process, and a
**daemon** that holds all the state and does all the work. Only the daemon knows about your
metrics source; the plugin and the bridge are thin.

## The daemon

The daemon is one Python process (`telemetry-nerd serve`) that serves everything over HTTP on a
single port (`127.0.0.1:7070` by default):

- The **REST API** the workspace runs on: querying and charting (`/api/query`,
  `/api/show`, `/api/compare-seasonal`, ...), the catalog, proposals, panels.
- Two **WebSockets**: `/ws` pushes live updates to the browser workspace (new panels, findings,
  hypothesis changes), and `/ws/bridge` is a separate channel for the pi editor extension.
- The **static UI** (the Svelte app you open in a browser) is served from the same process when
  it's been built in.
- An **MCP endpoint at `/mcp`**, served over streamable HTTP. This is the real MCP server: the
  tools Claude calls (`query`, `show`, `fleet`, `finding_create`, `run_code`, and the rest) are
  defined once, here, against the daemon's own `TelemetryService`.
- A **tier-2 code runner**: when a tool needs custom analysis, the daemon starts an IPython
  kernel subprocess with the query results handed in and the result (never raw rows) handed
  back.

Everything the daemon writes lives under one data directory (`TN_DATA_DIR`), as a few files, no
separate database server:

- `series.duckdb` — the series cache and dataset store: every series pulled from your source and
  every dataset a tool or `run_code` has produced, so a panel or a finding can be re-opened
  without re-querying the source.
- `workspace.db` — workspaces, panels, hypotheses, findings, the metric catalog and the event
  log, in SQLite.
- `daemon.json` — daemon-level settings (connected sources, etc).

One daemon, one data directory, one writer; the databases are single-writer, so two daemons never
share a directory.

## The plugin and the bridge

Claude Code never talks to the daemon's `/mcp` endpoint directly. Between them sits
`telemetry-nerd bridge`: a small stdio process that Claude Code starts itself, declared in the
plugin's `mcpServers` entry (`sh scripts/tn-launch bridge`). The bridge does three things:

1. **Fetches the tool list and forwards calls** to the daemon's `/mcp`, essentially verbatim —
   it is a thin proxy, not a second implementation of the tools.
2. **Delivers the live channel.** Things that happen in the browser workspace (a question asked
   from a selection, a hypothesis marked supported) arrive at the daemon over `/ws`, and the
   bridge relays them into the Claude Code session as `claude/channel` notifications, so Claude
   reacts to what you do in the UI without you having to describe it.
3. **Reconnects lazily.** If the daemon isn't reachable yet when the bridge starts, it still
   starts (with an empty tool list) and connects on the first real call, so a slow or
   not-yet-running daemon doesn't break the session.

Around the bridge, the rest of the plugin is static configuration and text, not a server:

- **Commands** (`/telemetry-nerd:start`, `:connect`, `:investigate`, `:learn`, `:open`, `:wrap`)
  are prompts with a fixed tool allowlist, run by Claude inside your session; `start.md` is
  typically the first one, and it ensures the daemon, connects a source, learns it and opens the
  workspace in one pass.
- **Skills** (`charting`, `evidence`, `metric-learning`, `model-views`, `tier2-code`, `triage`)
  are the operational rules Claude follows while using the tools: which chart answers which
  question, what counts as evidence, how to read a binding's verdict honestly, when to drop to
  custom code. They are how the design principles on the [rationale](/rationale/) page actually
  reach Claude's behaviour, rather than being asked for once and forgotten.
- **Hooks** run the launcher at two points: `SessionStart` (`tn-launch ensure`, which starts the
  daemon if nothing is reachable yet and reports the workspace URL as session context) and
  `UserPromptSubmit` (`tn-launch pending`, a lightweight check).

`scripts/tn-launch` is the one place all of this is resolved: it picks whichever of a `uv tool
install`, a source checkout, or an already-running daemon (`TN_DAEMON_URL`) is available, and
fails loudly with the install command if none is.

## Local vs. remote

The three pieces don't have to run on the same machine, and which of them move changes what you
need to set up.

**All local (the default).** Claude Code, the bridge and the daemon run on your machine. The
plugin starts the daemon on demand; you open the workspace in your own browser at
`127.0.0.1:7070`. Nothing to configure beyond pointing `TN_SOURCE_URL` at your metrics.

**Remote daemon, local Claude Code.** Run the daemon somewhere else — typically the container
image, since that's what's built and published — and point your local setup at it:

```bash
export TN_DAEMON_URL=http://<remote-host>:7070
```

The bridge then talks to the remote daemon over HTTP/WebSocket instead of starting a local one;
if it's unreachable, calls fail with an actionable error instead of silently falling back. The
bridge itself still runs locally and still needs the `telemetry-nerd` CLI installed, even though
it never starts a daemon of its own — only the heavy state and the metrics connection move to
the remote box.

The main reason to do this is proximity to the data, not convenience. The daemon is the thing
that queries your Prometheus/Thanos/VictoriaMetrics source, caches the results and runs analysis
over them; if that source is itself on a server (production, a VPC, a datacenter), running the
daemon next to it means every query is a local hop instead of a round trip from your laptop over
a VPN, and large pulls (a wide fleet query, a long seasonal window) don't cross the WAN twice —
once to the daemon, once again from the daemon to you, since the browser workspace only ever
receives the daemon's compact chart data, never the raw series. The other reason is sharing: one
daemon, one workspace, one catalog that a team investigates together instead of everyone caching
their own copy of the same metrics.

**Claude Code itself remote** (say, on a devbox or CI runner) is a variant of the same
arrangement: wherever Claude Code runs, that's where the bridge runs, and `TN_DAEMON_URL` points
it at the daemon, local to that box or remote again.

### What remote means for security

The daemon has **no authentication**, and that doesn't change when it's remote. Its Host/Origin
allowlist only accepts `127.0.0.1`, `localhost` and `[::1]` by default, specifically so that
publishing its port on anything other than loopback requires a deliberate choice
(`TN_ALLOWED_HOSTS`) rather than an accident. If you run it on a server, put it behind something
that authenticates — a reverse proxy, a tunnel, an SSH port-forward back to loopback — rather
than exposing `7070` directly. This is unchanged by anything above: Telemetry Nerd only reads
metrics, but the workspace itself (your investigation, your findings) has no access control of
its own yet.

There's a second thing to weigh, specific to this project: `run_code` means Claude can send it
arbitrary Python. That code runs in the daemon's IPython kernel subprocess, and the kernel is
**not sandboxed** — running code there is equivalent to running Python in Bash, on whichever
machine the daemon is on. Locally that's your own laptop, which you already trust with a shell.
Point `TN_DAEMON_URL` at a shared remote daemon and the same is true of that machine: anyone whose
Claude session is wired to it can run arbitrary code as the daemon's user. Treat the choice of
where the daemon runs as the choice of where you're letting Claude execute code, not just where
the metrics cache lives.

### A deployment skill, not yet built

A plugin skill that walks Claude through deploying the daemon to a remote server — picking
Docker Compose or Kubernetes, mounting the data volume, exposing the HTTP/WebSocket port behind
something that authenticates, pointing a local bridge's `TN_DAEMON_URL` at it, and confirming the
source shows up `live` in the workspace — is planned but doesn't exist yet. Until it lands, the
steps above are the manual version: build or pull the image, mount `/data`, publish the port
somewhere that isn't the open internet, and export `TN_DAEMON_URL`.

For the exact install commands, see the [getting started](/getting-started/) guide or
`docs/install.md` in the [repository](https://github.com/Fewbytes/telemetry-nerd).
