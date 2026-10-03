---
description: Connect a metrics source (registry name or URL) and report its status
argument-hint: "[url | registry-name]"
allowed-tools: mcp__plugin_telemetry-nerd_telemetry-nerd__public_sources, mcp__plugin_telemetry-nerd_telemetry-nerd__source_list, mcp__plugin_telemetry-nerd_telemetry-nerd__source_connect, mcp__plugin_telemetry-nerd_telemetry-nerd__source_status, mcp__plugin_telemetry-nerd_telemetry-nerd__source_disconnect
---

Connect a metrics source. Argument: `$ARGUMENTS` (a URL or a registry name; may be empty).

1. If empty, call `source_list` and `public_sources` and ask what to connect (name or URL).
2. A name listed by `public_sources`: `source_connect(name=...)` alone; the registry supplies url,
   flavor, resolution and politeness. These are shared third-party servers: interactive volume
   only, narrow queries.
3. A URL: pick a short `name`; `source_connect(name, url, flavor)` with flavor `victoriametrics`
   only when known to be VM, else `prometheus`. A Grafana page URL is not a source: the Grafana
   front door (bead telemetry-nerd-3fs.2) is not built; ask for a datasource proxy URL
   (`https://<grafana>/api/datasources/proxy/uid/<uid>`) or a direct Prometheus URL.
4. Name already taken: ask before passing `replace=true`.
5. Never ask for or pass a token; use `auth_file` (absolute path) or `auth_env` (variable name).
6. Then `source_status(name)` and report in two lines: reachable, latency, application/version,
   flavor, and any error with its hint. Suggest `/telemetry-nerd:learn <name>` (or `/telemetry-nerd:start` for the full
   flow) as the next step. Do not learn the source here.
