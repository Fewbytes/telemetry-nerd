# Installing Telemetry Nerd

Two supported paths: `uv tool install` (local) or the container image.

## uv

Requires [uv](https://docs.astral.sh/uv/) and, for source installs, node/npm (the wheel
build compiles the Svelte UI and bundles it as `telemetry_nerd/ui_dist`; see
`hatch_build.py`).

```bash
uv tool install git+https://github.com/<owner>/telemetry-nerd   # or a local path / built wheel
telemetry-nerd serve        # UI + API + MCP at http://127.0.0.1:7070
```

### Optional `analysis` extra

Tier-2 code (`run_code`, an IPython kernel subprocess) always has numpy, polars, duckdb and
pyarrow. The `analysis` extra adds scipy, statsmodels, scikit-learn, ruptures and pywavelets
(about 170 MB installed, plus pandas as a statsmodels dependency):

```bash
uv tool install 'telemetry-nerd[analysis]'      # or 'git+https://...#egg=telemetry-nerd[analysis]'
```

Skip it if you only use the built-in analysis tools; add it for statistical tests, regression,
clustering, change-point detection (ruptures) or wavelets in kernel code. ruptures has no
Python 3.14 wheel yet, so installing the extra on 3.14 compiles it (needs a C compiler).

Data lives in `$TN_DATA_DIR` (default `~/.local/share/telemetry-nerd`). Point it at a
metrics source with `TN_SOURCE_URL` (default `http://127.0.0.1:8428`) and `TN_SOURCE_FLAVOR`
(`victoriametrics` | `prometheus`).

Building a wheel without node: `TN_SKIP_UI_BUILD=1 uv build --wheel` gives an API-only wheel
(the daemon warns that the UI is missing). `just verify-install` builds a wheel, checks it
contains the UI and no dev-only deps, installs it into throwaway dirs and smoke-tests `serve`.

## Container (podman or docker)

### Run the published image

Images are built by CI for `linux/amd64` and `linux/arm64` and pushed to GHCR **only for version
tags** (`v*`; the tag must match the version in `pyproject.toml`). Regular commits never publish
one, so a version that was just tagged may take a few minutes to appear. Tags
(`ghcr.io/fewbytes/telemetry-nerd`):

| Variant | Tags | Contents |
| --- | --- | --- |
| slim | `X.Y.Z`, `X.Y`, `latest` | daemon + IPython kernel deps (~560 MB) |
| full | `X.Y.Z-full`, `X.Y-full`, `latest-full` | slim + the `analysis` extra (~800 MB) |

Use slim unless kernel code needs scipy / statsmodels / scikit-learn / ruptures / pywt; use
full when it does (nothing can be pip-installed at runtime: the kernel has no network policy
and the image runs as an unprivileged user).

```bash
podman volume create tn-data            # once; holds all state (see below)
podman run -d --name telemetry-nerd \
  -p 127.0.0.1:7070:7070 \
  -v tn-data:/data \
  -e TN_SOURCE_URL=http://host.containers.internal:8428 \
  ghcr.io/fewbytes/telemetry-nerd:latest
# then browse http://127.0.0.1:7070
```

`docker` works the same (`docker volume create`, `docker run ...`; on Linux use
`--add-host=host.docker.internal:host-gateway` and `http://host.docker.internal:8428` to reach a
source on the host). `latest` moves with every release: pin `X.Y.Z` (or `X.Y` for patch updates)
for anything you keep.

Claude's analysis code (the tier-2 `run_code` tools) runs as IPython kernel subprocesses inside
this container; there is no separate sandbox image. The libraries it can import are the ones in the
image, which is the whole difference between the slim and the `-full` variant.

### Data volume

The image contains no data. Everything the daemon writes lives under `/data`
(`TN_DATA_DIR=/data`): the series cache and dataset store (`series.duckdb`), workspaces,
catalog and event log (`workspace.db`) and `daemon.json`. Always mount a volume there; without
one, state lives in the container's writable layer and is lost with `--rm` or on upgrade.

- **Named volume (recommended):** `-v tn-data:/data`. The engine creates it owned by the
  image's user, so permissions just work.
- **Bind mount:** `-v /path/on/host:/data`. The daemon runs as uid `10001` (user `tn`), so the
  directory must be writable by it: `mkdir -p /path/on/host && sudo chown 10001:10001
  /path/on/host`. Rootless podman maps uids, so use `podman unshare chown 10001:10001
  /path/on/host` instead; on SELinux hosts add `:Z` (`-v /path/on/host:/data:Z`).
- **One daemon per volume.** The databases are single-writer: don't mount the same volume
  into two running containers.
- **Upgrade:** `podman pull ghcr.io/fewbytes/telemetry-nerd:<new>`, stop and remove the old
  container, start the new one with the same `-v tn-data:/data`. State carries over. Switching
  between the slim and `-full` variant is the same operation: the volume is shared.
- **Back up:** stop the container, then
  `podman run --rm -v tn-data:/data -v "$PWD":/backup docker.io/library/busybox tar czf /backup/tn-data.tgz -C /data .`
  (restore with `tar xzf` into an empty volume the same way).
- **Reset:** stop the container and `podman volume rm tn-data`.

### Build locally

```bash
just docker-build                      # CONTAINER=docker just docker-build for docker
just docker-build-full                 # + analysis extra, tagged telemetry-nerd:dev-full
TN_SOURCE_URL=http://host.containers.internal:8428 just docker-run   # uses the tn-data volume
```

- Runs as non-root user `tn` (uid 10001).
- `EXPOSE 7070`; `HEALTHCHECK` polls `/api/health`. With podman the image must be built with
  `--format docker` (the recipe does this) or the healthcheck is dropped.
- The build strips debug symbols from native extensions and drops pyarrow's development files
  (about 90 MB smaller); local state (`.tn-data`, `*.duckdb`, `*.db`) is excluded by
  `.dockerignore`.
- Env: `TN_SOURCE_URL`, `TN_SOURCE_FLAVOR`, `TN_PORT`, `TN_DATA_DIR`, `TN_HOST`
  (image default `0.0.0.0`), `TN_ALLOWED_HOSTS` (comma-separated extra Host/Origin names).

### Network security

The daemon has no authentication. Outside the container it binds `127.0.0.1`. Inside the
image `TN_HOST=0.0.0.0` is required (a loopback bind is unreachable through a published
port), but the Host/Origin allowlist (DNS-rebinding and cross-site WebSocket protection) is
**unchanged**: only `127.0.0.1`, `localhost` and `[::1]` are accepted. So publish the port on
loopback (`-p 127.0.0.1:7070:7070`) and browse `http://127.0.0.1:7070`. To reach it by another
name set `TN_ALLOWED_HOSTS=name`, and only then put it behind something that authenticates.

## Claude Code plugin

The plugin (`.claude-plugin/`, `.mcp.json`, `hooks/hooks.json`) starts everything through one
launcher, `scripts/tn-launch`, which picks the first install it finds:

1. `TN_USE_SOURCE=1` and a source checkout at the plugin root: `uv run --directory <root>`.
2. `telemetry-nerd` on `PATH` (`uv tool install ...`).
3. A source checkout at the plugin root (needs `uv`): `uv run --directory <root>`.
4. Otherwise it fails loudly (exit 127, stderr: how to install). The `ensure` hook reports the
   same message as session context and never blocks the session.

So for a marketplace install, `uv tool install` first, then `/plugin marketplace add
Fewbytes/telemetry-nerd` and `/plugin install telemetry-nerd@telemetry-nerd`.

### Commands

| Command | What it does |
|---|---|
| `/telemetry-nerd:start [url\|name]` | One entry point: ensures the daemon, connects your data (Prometheus-compatible URL, a Grafana URL whose datasources are discovered and connected by uid, or a public registry name), learns it, prints the workspace URL, what was learned (metrics, packs, binding suggestions) and the approved lessons that apply. |
| `/telemetry-nerd:connect [url\|name]` | Connect a source and report its status. |
| `/telemetry-nerd:investigate <question>` | Scope the question, record a hypothesis, follow the `triage` skill; claims need evidence. |
| `/telemetry-nerd:open` | Print (and try to open) the workspace UI URL. |
| `/telemetry-nerd:learn [source] [prefix]` | Learn what the source's metrics mean and record it in the catalog. |
| `/telemetry-nerd:wrap` | End of an investigation: propose catalog updates and scoped lessons; you approve, edit or reject them in the UI's Proposals view. |

Commands are namespaced by the plugin name (`/telemetry-nerd:<command>`); Claude Code also
accepts the bare form (e.g. `/start`) when no other plugin claims it.

### Working in this repo as a local checkout (not marketplace-installed)

A marketplace install (`/plugin install telemetry-nerd@telemetry-nerd`) has Claude Code merge
`hooks/hooks.json` and `.mcp.json` for you. Working directly in a source checkout of *this*
repo (e.g. as the `telemetry-nerd@inline` dev loop) only picks up `.mcp.json` automatically —
Claude Code does not merge an unregistered checkout's `hooks/hooks.json`. Without it, the
`UserPromptSubmit` hook that drains queued UI events into context never runs, and workspace
chat messages sit "queued" until something calls `pending` by hand.

Wire it into `.claude/settings.json` yourself, using `$CLAUDE_PROJECT_DIR` (not
`${CLAUDE_PLUGIN_ROOT}`, which only resolves inside an actual plugin context):

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command",
        "command": "sh \"$CLAUDE_PROJECT_DIR/scripts/tn-launch\" ensure", "timeout": 30 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "sh \"$CLAUDE_PROJECT_DIR/scripts/tn-launch\" pending", "timeout": 10 } ] }
    ]
  }
}
```

### Channel vs. hook delivery

Workspace UI events reach Claude Code one of two ways (`core/presence.py`):

- **channel** (push): live over the bridge's `/ws/bridge` socket, the moment an event happens.
  Requires the connecting MCP client to advertise the experimental `claude/channel` capability
  during initialize, or `TN_CHANNEL=1` in the bridge's environment.
- **hook** (poll): the default today, since Claude Code doesn't advertise `claude/channel` yet.
  The `UserPromptSubmit` hook (`tn-launch pending`) claims and prints whatever queued since your
  last prompt. No live push, but no unread backlog either — it drains on every turn.

**If/when Anthropic ships `claude/channel` support in Claude Code, no action is needed here.**
The bridge already detects it automatically: `client_advertised_channel()` in
`bridge/proxy.py` checks `ClientCapabilities.experimental` on every session and calls
`gate.enable()` the moment it sees the capability, switching that session from `hook` to
`channel` mode on the spot. `TN_CHANNEL=1` exists only to force the same switch early, e.g. for
testing against a client build that supports it but doesn't advertise it yet.

### Plugin against a container daemon

The bridge is a small stdio process that still needs the `telemetry-nerd` CLI (install 2 above);
the daemon it talks to can be the container. Set `TN_DAEMON_URL` in the environment Claude Code
starts with:

```bash
export TN_DAEMON_URL=http://127.0.0.1:7070   # container published with -p 127.0.0.1:7070:7070
```

With it set, the bridge and hooks use that daemon and never autostart a local one; if it is not
healthy the bridge exits with an actionable error. Equivalent manual form:
`telemetry-nerd bridge --daemon-url http://127.0.0.1:7070`.
