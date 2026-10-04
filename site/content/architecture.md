---
title: "Architecture"
description: "How the Claude Code plugin, the MCP bridge and the daemon fit together, and how to run Telemetry Nerd locally or on a remote box."
---

Telemetry Nerd is three pieces talking to each other: a Claude Code **plugin** (skills, commands
and hooks, running inside your Claude Code session), a small **MCP bridge** process, and a
**daemon** that holds all the state and does all the work. Only the daemon knows about your
metrics source; the plugin and the bridge are thin.

## How the pieces are wired

<div class="diagram">
<svg viewBox="0 0 900 460" role="img" aria-label="Architecture diagram: Claude Code's plugin talks over stdio to the MCP bridge, which calls the daemon's /mcp endpoint over HTTP; the browser talks to the daemon over HTTP and WebSocket; the daemon queries the metrics source, spawns a tier-2 kernel subprocess for run_code, and reads and writes two local database files, series.duckdb and workspace.db.">
<defs>
<marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0,0 L10,5 L0,10 z" fill="var(--ink-soft)"></path>
</marker>
</defs>
<rect class="box" x="20" y="20" width="200" height="70" rx="6"></rect>
<text class="t" x="120" y="48" text-anchor="middle">Claude Code</text>
<text class="st" x="120" y="66" text-anchor="middle">plugin: skills, commands, hooks</text>
<rect class="box" x="20" y="150" width="200" height="60" rx="6"></rect>
<text class="t" x="120" y="176" text-anchor="middle">MCP bridge</text>
<text class="st" x="120" y="192" text-anchor="middle">stdio, spawned by Claude Code</text>
<rect class="box" x="20" y="280" width="200" height="60" rx="6"></rect>
<text class="t" x="120" y="306" text-anchor="middle">Browser</text>
<text class="st" x="120" y="322" text-anchor="middle">workspace UI</text>
<rect class="group" x="280" y="20" width="340" height="240" rx="8"></rect>
<text class="t" x="450" y="40" text-anchor="middle">Daemon — telemetry-nerd serve</text>
<rect class="box" x="300" y="55" width="300" height="30" rx="4"></rect>
<text class="st" x="450" y="74" text-anchor="middle">REST API (/api/...)</text>
<rect class="box" x="300" y="95" width="300" height="30" rx="4"></rect>
<text class="st" x="450" y="114" text-anchor="middle">MCP endpoint (/mcp)</text>
<rect class="box" x="300" y="135" width="300" height="30" rx="4"></rect>
<text class="st" x="450" y="154" text-anchor="middle">WebSockets (/ws, /ws/bridge)</text>
<rect class="box" x="300" y="175" width="300" height="30" rx="4"></rect>
<text class="st" x="450" y="194" text-anchor="middle">run_code → tier-2 kernel (below)</text>
<rect class="box" x="680" y="20" width="190" height="70" rx="6"></rect>
<text class="t" x="775" y="48" text-anchor="middle">Metrics source</text>
<text class="st" x="775" y="66" text-anchor="middle">Prometheus / VM / Thanos</text>
<rect class="box dashed" x="370" y="300" width="160" height="50" rx="6"></rect>
<text class="t" x="450" y="320" text-anchor="middle">tier-2 kernel</text>
<text class="st" x="450" y="336" text-anchor="middle">run_code, unsandboxed</text>
<rect class="box db" x="260" y="370" width="160" height="60" rx="6"></rect>
<text class="t" x="340" y="396" text-anchor="middle">series.duckdb</text>
<text class="st" x="340" y="412" text-anchor="middle">series cache, datasets</text>
<rect class="box db" x="560" y="370" width="160" height="60" rx="6"></rect>
<text class="t" x="640" y="396" text-anchor="middle">workspace.db</text>
<text class="st" x="640" y="412" text-anchor="middle">workspace, catalog, events</text>
<line class="edge" x1="120" y1="90" x2="120" y2="150" marker-end="url(#arrow)"></line>
<rect class="lblbg" x="124" y="112" width="60" height="16"></rect>
<text class="lbl" x="128" y="123">spawns</text>
<polyline class="edge" points="220,170 260,170 260,110 280,110" marker-end="url(#arrow)"></polyline>
<rect class="lblbg" x="205" y="132" width="90" height="16"></rect>
<text class="lbl" x="250" y="143" text-anchor="middle">/mcp (HTTP)</text>
<polyline class="edge" points="220,300 250,300 250,150 280,150" marker-end="url(#arrow)"></polyline>
<rect class="lblbg" x="208" y="214" width="84" height="16"></rect>
<text class="lbl" x="250" y="225" text-anchor="middle">HTTP + WS</text>
<polyline class="edge" points="620,70 650,70 650,55 680,55" marker-end="url(#arrow)"></polyline>
<rect class="lblbg" x="606" y="27" width="88" height="16"></rect>
<text class="lbl" x="650" y="38" text-anchor="middle">HTTP query</text>
<line class="edge" x1="450" y1="260" x2="450" y2="300" marker-end="url(#arrow)"></line>
<rect class="lblbg" x="433" y="273" width="60" height="16"></rect>
<text class="lbl" x="463" y="284" text-anchor="middle">spawns</text>
<line class="edge" x1="340" y1="260" x2="340" y2="370" marker-end="url(#arrow)"></line>
<rect class="lblbg" x="233" y="309" width="104" height="16"></rect>
<text class="lbl" x="335" y="320" text-anchor="end">reads / writes</text>
<polyline class="edge" points="600,260 600,335 640,335 640,370" marker-end="url(#arrow)"></polyline>
<rect class="lblbg" x="604" y="289" width="104" height="16"></rect>
<text class="lbl" x="608" y="300">reads / writes</text>
</svg>
<p class="diagram-caption">The daemon is the only piece that talks to your metrics source or touches disk. <code>series.duckdb</code> and <code>workspace.db</code> are plain files it reads and writes directly — no separate database server sits between them.</p>
</div>

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
