# INSTALL.md — Telemetry Nerd install runbook for agents

Audience: an AI coding agent installing Telemetry Nerd for a user, non-interactively.
Every step below has a literal command and a literal expected output to check. If an
actual output doesn't match, stop and use the matching entry in **Troubleshooting**
instead of guessing.

This is the agent runbook. `docs/install.md` is the longer human-oriented reference
(same facts, more prose, more options); link the user there if they want to read more.
`README.md` has a short human-facing "Getting started" section that should stay
consistent with this file but not duplicate its detail.

Do not improvise flags, URLs, or paths not shown here. Do not pass secrets as CLI
arguments or tool call arguments — see step 5.

---

## 0. Prerequisites check

Run these in order. Stop and report if any fails before proceeding.

```bash
uv --version
```
Expected: a line like `uv 0.x.y` (any version). If this fails, uv is not installed —
see Troubleshooting "`uv` not found".

```bash
claude --version
```
Expected: a line like `1.x.y (Claude Code)`. Required for MCP registration (step 3).

Only if you will build from source rather than installing a published wheel/image
(the `uv tool install git+...` path below builds from source and needs these too):

```bash
node --version && npm --version
```
Expected: two version lines, e.g. `v26.x.x` and `10.x.x`. Node builds the Svelte UI
that gets bundled into the wheel (`hatch_build.py`). If missing, use the Docker path
instead (step 2B), which builds the UI inside the image.

For the Docker path only:

```bash
docker --version || podman --version
```
Expected: a version line from whichever is installed. Either works; examples below use
`docker`, substitute `podman` 1:1.

---

## 1. Choose an install path

- **uv tool install** (path A): fastest to verify, runs natively, needs node/npm to
  build from source. Use this unless the user specifically wants containers.
- **Docker/Podman image** (path B): no local node/uv/Python needed beyond the
  container runtime; image is `ghcr.io/fewbytes/telemetry-nerd`, published only on
  version tags (e.g. `0.0.1`, `latest`). Use this when the host can't/shouldn't have a
  Python toolchain, or the user explicitly asks for a container.

Both paths end with the same MCP registration (step 3) and verification (step 5).

---

## 2A. Path A — `uv tool install`

```bash
uv tool install git+https://github.com/Fewbytes/telemetry-nerd
```
Expected: ends with a line like
`Installed 1 executable: telemetry-nerd` (exact wording may vary by uv version; a
non-zero exit code or a line containing `error` means it failed).

```bash
telemetry-nerd --help
```
Expected: usage output starting with `usage: telemetry-nerd` and listing subcommands
`serve` and `bridge`.

Data is stored under `$TN_DATA_DIR` (default `~/.local/share/telemetry-nerd`). No
further action needed for a plugin install — the plugin's launcher (step 3) starts the
daemon on demand. To start the daemon directly instead:

```bash
telemetry-nerd serve
```
Expected (stays running in foreground; run with `&`/`nohup`/a process manager if you
need the shell back): a log line containing `Uvicorn running on http://127.0.0.1:7070`.

## 2B. Path B — Docker/Podman image

Pull the published image:

```bash
docker pull ghcr.io/fewbytes/telemetry-nerd:latest
```
Expected: ends with `Status: Downloaded newer image for
ghcr.io/fewbytes/telemetry-nerd:latest` (or `Image is up to date` on a re-pull).

If the pull fails with `manifest unknown` or `not found` (no tagged image has been
published yet — the image is only built on version-tag pushes), build it locally
instead from a checkout of the repo:

```bash
git clone https://github.com/Fewbytes/telemetry-nerd && cd telemetry-nerd
docker build -t telemetry-nerd:dev .
```
Expected: ends with `Successfully tagged` or (buildx) a `writing image` line with no
error. Use image name `telemetry-nerd:dev` in place of
`ghcr.io/fewbytes/telemetry-nerd:latest` in the commands below if you built locally.

Run it:

```bash
docker run -d --name telemetry-nerd -p 127.0.0.1:7070:7070 -v tn-data:/data \
  -e TN_SOURCE_URL="${TN_SOURCE_URL:-http://host.docker.internal:8428}" \
  ghcr.io/fewbytes/telemetry-nerd:latest
```
Expected: a single 64-character container ID printed to stdout, nonzero exit = failure.

```bash
docker ps --filter name=telemetry-nerd --format '{{.Status}}'
```
Expected: a line starting with `Up` (after the 10s start period, it should say
`Up ... (healthy)`).

Notes:
- The container always binds `0.0.0.0` internally; the published port is restricted to
  `127.0.0.1` on the host side (`-p 127.0.0.1:7070:7070`), and the daemon's own
  Host/Origin allowlist only accepts `127.0.0.1`/`localhost`/`[::1]` regardless. Never
  publish this port on a non-loopback interface — there is no authentication.
- With Podman, add `--format docker` to `docker build` or the `HEALTHCHECK` is dropped
  from the image and step 5's `docker ps` health check won't show `(healthy)`.
- To reach a source running on the Docker host itself, use
  `http://host.docker.internal:8428` (Docker Desktop/Podman) as shown above; on Linux
  Docker Engine you may need `--add-host=host.docker.internal:host-gateway`.

---

## 3. MCP registration

Two ways to register the MCP server. Use (a) for a normal user-facing install; use (b)
only if the user explicitly wants the MCP server registered without the plugin
(no slash commands, no session hooks).

### 3a. Via the Claude Code plugin (preferred)

Run inside a Claude Code session (these are Claude Code slash commands, not shell
commands):

```
/plugin marketplace add Fewbytes/telemetry-nerd
/plugin install telemetry-nerd@telemetry-nerd
```
Expected: Claude Code reports the plugin installed successfully and lists
`telemetry-nerd` under installed plugins. This registers the MCP server defined in the
repo's `.mcp.json`:

```json
{
  "mcpServers": {
    "telemetry-nerd": {
      "command": "sh",
      "args": ["${CLAUDE_PLUGIN_ROOT:-.}/scripts/tn-launch", "bridge"],
      "env": { "TN_SOURCE_URL": "${TN_SOURCE_URL:-http://127.0.0.1:8428}" }
    }
  }
}
```

`scripts/tn-launch` resolves, in order: (1) `TN_USE_SOURCE=1` + a source checkout at
the plugin root, (2) `telemetry-nerd` on `PATH` (the path A install satisfies this),
(3) a source checkout with `uv` available, (4) fails loudly with an install hint on
stderr/exit 127. So for path A, the plugin works immediately after `uv tool install`.
For path B (container-only, no local `telemetry-nerd` CLI), you still need the CLI
locally for the bridge — see "Plugin against a container daemon" below.

### 3b. Manual registration (no plugin)

```bash
claude mcp add telemetry-nerd -e TN_SOURCE_URL=http://127.0.0.1:8428 -- telemetry-nerd bridge
```
Expected: `Added stdio MCP server telemetry-nerd to local config`. Requires
`telemetry-nerd` to already be on `PATH` (path A), since there is no `${CLAUDE_PLUGIN_ROOT}`
launcher to fall back to a source checkout in this mode.

Verify it registered:

```bash
claude mcp list
```
Expected: a line containing `telemetry-nerd` with the command shown above.

### Plugin against a container daemon (path B + plugin)

The MCP bridge is a stdio process and still needs the `telemetry-nerd` CLI even when
the daemon itself runs in a container (install it with path A alongside path B, or run
the bridge from a source checkout). Point the bridge at the container's daemon instead
of autostarting a local one:

```bash
export TN_DAEMON_URL=http://127.0.0.1:7070
```
Set this in the environment Claude Code is launched from before registering/using the
MCP server. With it set, the bridge never autostarts a local daemon; if the container
isn't healthy yet, the bridge exits with an actionable stderr message instead of hanging.

---

## 4. Data source connect

Metrics come from a Prometheus-compatible source (Prometheus, VictoriaMetrics, Thanos,
Mimir, or a Grafana datasource proxy). Connect it with the `source_connect` MCP tool —
do this from within a Claude Code session that has the `telemetry-nerd` MCP server
registered (step 3).

**No source available yet / just verifying the install** — connect a public demo
source by name (no URL, no auth, already in the registry):

```
source_connect(name="grafana-play")
```
Expected return: `{"source": {"name": "grafana-play", ...}, "status": {"reachable": true, ...}}`.

**User's own source** — concrete example against a local VictoriaMetrics on the
default port:

```
source_connect(
  name="prod",
  url="http://127.0.0.1:8428",
  flavor="victoriametrics",
  resolution="15s"
)
```
Expected return: `{"source": {"name": "prod", "url": "http://127.0.0.1:8428", "flavor": "victoriametrics", ...}, "status": {"reachable": true, ...}}`.
If `status.reachable` is `false`, see Troubleshooting "source_connect unreachable".

Rules, not suggestions:
- `flavor` is `"victoriametrics"` (enables MetricsQL `rollup()`) or `"prometheus"`
  (also correct for Thanos/Mimir/vanilla Prometheus).
- `url` is the API base **without** `/api/v1` (e.g. `http://prometheus:9090`, a VM
  `/select/0/prometheus` path, or `https://<grafana>/api/datasources/proxy/uid/<uid>`).
- **Never** pass a token/password as a tool argument. If the source needs auth, ask
  the user to put the secret in a file and pass `auth_file=<absolute path>` (read
  immediately, no restart needed), or set an environment variable on the daemon and
  pass `auth_env=<VAR_NAME>` (needs a daemon restart if set after the daemon started).
  `auth_scheme` is `"bearer"` (default) or `"basic"` (value `"user:pass"`).
- Connecting to a shared/public server: lower `max_concurrency` (default 4), raise
  `min_interval` (e.g. `"500ms"`) and `timeout` (e.g. `"60s"`) to be polite.
- To reconnect/change settings for a name that already exists, pass `replace=true`.

---

## 5. Verification

Run every check below. All must pass for the install to count as successful.

**5.1 Daemon HTTP health** (works for both path A `telemetry-nerd serve` and path B
container):
```bash
curl -s http://127.0.0.1:7070/api/health
```
Expected: `{"ok":true,"version":"<version>","last_seq":<integer>}` (jq not required;
just confirm `"ok":true` is present).

**5.2 Workspace UI loads**:
```bash
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7070/
```
Expected: `200`.

**5.3 MCP server responds** (inside a Claude Code session with the server registered
per step 3):
```
source_list()
```
Expected return: `{"sources": [...]}`. An empty list is fine before step 4; after
step 4 it must include the entry you connected, e.g.
`{"sources": [{"name": "grafana-play", ...}]}`.

**5.4 Source connect round-trip** (if step 4 was run):
```
source_status(name="grafana-play")
```
Expected return: `{"reachable": true, ...}` (no `error` key).

**5.5 Container health (path B only)**:
```bash
docker inspect --format '{{.State.Health.Status}}' telemetry-nerd
```
Expected: `healthy` (allow up to the 10s `start-period` before this is meaningful).

If all five pass: report success, including which path (A/B) and whether a data
source was connected. If any fails: do not report success — go to Troubleshooting.

---

## 6. Troubleshooting

**`uv` not found** — `uv --version` fails with "command not found". Install it:
`curl -LsSf https://astral.sh/uv/install.sh | sh`, then re-open the shell or
`source $HOME/.local/bin/env`, then retry step 0.

**`uv tool install` fails with a node/npm build error** — the wheel build compiles the
UI and needs node/npm. Either install node (`node --version` must succeed, step 0), or
build an API-only wheel with `TN_SKIP_UI_BUILD=1 uv build --wheel` and install that
(the daemon will log a warning that the UI is missing but the MCP tools still work),
or switch to path B (Docker builds the UI in its own stage, no local node needed).

**`telemetry-nerd: command not found` after `uv tool install`** — the uv tool bin dir
isn't on `PATH`. Run `uv tool update-shell`, then open a new shell, then retry
`telemetry-nerd --help`.

**`docker pull ghcr.io/fewbytes/telemetry-nerd:latest` → `manifest unknown` /
`not found`** — no version tag has been published yet (the image is built only on
`vX.Y.Z` tag pushes, see `.github/workflows/ci.yml`). Build locally instead (path B,
"build it locally" block).

**`docker run` container exits immediately / `docker ps` shows no entry** — check logs:
```bash
docker logs telemetry-nerd
```
Common cause: port 7070 already in use on the host. Fix: stop whatever is using it, or
remap with `-p 127.0.0.1:7071:7070` and use that port in step 5's URLs instead.

**`docker inspect ... Health.Status` stays `starting` or never becomes `healthy`** —
wait past the 10s start period, then re-run. If still unhealthy with Podman, you built
without `--format docker`: rebuild with `podman build --format docker -t <image> .`
(the `HEALTHCHECK` instruction is otherwise dropped).

**`curl http://127.0.0.1:7070/api/health` connection refused** — the daemon isn't
running or isn't listening where you expect. Path A: run `telemetry-nerd serve` in the
foreground and watch for the `Uvicorn running on ...` line / any traceback. Path B:
check `docker ps` and `docker logs telemetry-nerd`. If using the plugin's on-demand
launcher, trigger it by opening a Claude Code session (SessionStart hook runs
`tn-launch ensure`) and check the session's startup context for an error line.

**MCP server not available in Claude Code (`source_list` etc. not callable)** —
`claude mcp list` (3b) or `/plugin list` (3a) to confirm registration; if registered
but tools are missing, the bridge process failed to start. Run
`sh scripts/tn-launch bridge` directly in a shell (from a repo checkout, or with
`CLAUDE_PLUGIN_ROOT` set to the plugin's install dir) — a clean failure message on
stderr (e.g. "cannot find telemetry-nerd") tells you which install step to redo.

**`source_connect` unreachable** — `status.reachable` is `false` or the tool raises.
Check:
1. `url` has no trailing `/api/v1` and no trailing slash mismatch.
2. The source is actually reachable from where the daemon runs: from the daemon's
   host/container, `curl -s <url>/api/v1/query?query=up | head -c 200` should return
   JSON, not a connection error. In path B, `127.0.0.1` inside the container is the
   container itself — use `host.docker.internal` (or `--add-host`) to reach the
   Docker host.
3. `flavor` matches the server (VictoriaMetrics needs `"victoriametrics"` to use
   `rollup()`; everything else, including Thanos/Mimir, is `"prometheus"`).
4. If auth is required and `auth_env` was used, the daemon must be restarted after the
   env var was set for it to pick up the new variable.

**Plugin starts but points at the wrong daemon (path B)** — confirm `TN_DAEMON_URL`
is set in the environment Claude Code was launched from (not just in the current
shell if Claude Code was started earlier/elsewhere), then restart the Claude Code
session so the bridge picks it up.

**Version mismatch / "which version did I install"**:
```bash
telemetry-nerd --help 2>&1 | head -1
curl -s http://127.0.0.1:7070/api/health
```
The health endpoint's `"version"` field is authoritative for whatever daemon is
actually serving traffic (path A local process or path B container).
