# DevOpsDays TLV 2026 — Telemetry Nerd hands-on: before you arrive

90-minute hands-on session. Please set up **before** the session; venue Wi-Fi is shared by
everyone and installs are slow there. About 15 minutes.

## You need

- A laptop (macOS, Linux, or Windows with WSL2)
- [Claude Code](https://claude.com/claude-code), logged in with an account that can use it
- **Either** [uv](https://docs.astral.sh/uv/) **or** Docker / Podman
- Outbound HTTPS to the workshop Grafana (URL announced before the session)
- Optional, as a fallback for bad Wi-Fi: Docker/Podman to run the lab data locally
  (`just workshop-up` from a clone of the repo, ~400 MiB RAM)

## Install

### Option A — uv

```bash
uv tool install git+https://github.com/Fewbytes/telemetry-nerd
telemetry-nerd --help
```

### Option B — container

```bash
docker run -d --name telemetry-nerd \
  -p 127.0.0.1:7070:7070 -v tn-data:/data \
  ghcr.io/fewbytes/telemetry-nerd:latest
```

Then start Claude Code with `TN_DAEMON_URL=http://127.0.0.1:7070` so it talks to the container.

### Then, in Claude Code (both options)

```
/plugin marketplace add Fewbytes/telemetry-nerd
/plugin install telemetry-nerd@telemetry-nerd
```

## Check it works

Ask Claude:

> Connect to grafana-play and tell me how many metrics it has.

(The workshop source, `devopsdays`, goes live the day before; grafana-play is only the smoke test.)

Then open <http://127.0.0.1:7070>. If you see the workspace and Claude answered, you're ready.

## Stuck?

Come 10 minutes early; helpers will be at the front of the room. Or open an issue on
<https://github.com/Fewbytes/telemetry-nerd/issues>.
