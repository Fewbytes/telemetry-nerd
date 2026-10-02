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

Data lives in `$TN_DATA_DIR` (default `~/.local/share/telemetry-nerd`). Point it at a
metrics source with `TN_SOURCE_URL` (default `http://127.0.0.1:8428`) and `TN_SOURCE_FLAVOR`
(`victoriametrics` | `prometheus`).

Building a wheel without node: `TN_SKIP_UI_BUILD=1 uv build --wheel` gives an API-only wheel
(the daemon warns that the UI is missing). `just verify-install` builds a wheel, checks it
contains the UI and no dev-only deps, installs it into throwaway dirs and smoke-tests `serve`.

## Container (podman or docker)

```bash
just docker-build                      # CONTAINER=docker just docker-build for docker
TN_SOURCE_URL=http://host.containers.internal:8428 just docker-run
# or directly:
podman run --rm -p 127.0.0.1:7070:7070 -v tn-data:/data -e TN_SOURCE_URL=... telemetry-nerd:dev
```

- Runs as non-root user `tn`; state is in the `/data` volume (replaces `.tn-data`).
- `EXPOSE 7070`; `HEALTHCHECK` polls `/api/health`. With podman the image must be built with
  `--format docker` (the recipe does this) or the healthcheck is dropped.
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
