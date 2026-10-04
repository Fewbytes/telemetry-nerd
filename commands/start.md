---
description: Get going - make sure the daemon runs, connect your data, learn it, open the workspace
argument-hint: "[prometheus-url | registry-name | grafana-url]"
allowed-tools: Bash(sh ${CLAUDE_PLUGIN_ROOT}/scripts/tn-launch *), mcp__plugin_telemetry-nerd_telemetry-nerd__source_list, mcp__plugin_telemetry-nerd_telemetry-nerd__source_discover_grafana, mcp__plugin_telemetry-nerd_telemetry-nerd__lessons_for, mcp__plugin_telemetry-nerd_telemetry-nerd__workspace_list, mcp__plugin_telemetry-nerd_telemetry-nerd__source_connect, mcp__plugin_telemetry-nerd_telemetry-nerd__source_status, mcp__plugin_telemetry-nerd_telemetry-nerd__source_learn, mcp__plugin_telemetry-nerd_telemetry-nerd__public_sources, mcp__plugin_telemetry-nerd_telemetry-nerd__binding_suggest
---

Start a Telemetry Nerd session. Data location given: `$ARGUMENTS` (may be empty).

1. **Daemon.** The SessionStart hook already ran `scripts/tn-launch ensure`; its context line holds
   the workspace UI URL (default http://127.0.0.1:7070). If you cannot see it, run
   `sh ${CLAUDE_PLUGIN_ROOT}/scripts/tn-launch ensure` (exactly this form, so the allowed-tools rule matches). If that reports the daemon missing or
   telemetry-nerd not installed, relay its message (see docs/install.md) and stop. Report the URL.
2. **Where is the data?** Call `source_list` first; if a live source already covers it, say so
   and skip to step 4. Otherwise, from `$ARGUMENTS`:
   - empty: ask the user: a Prometheus / VictoriaMetrics / Thanos / Mimir URL, a Grafana URL, or
     a public demo source (show `public_sources` and offer them by name);
   - a name in `public_sources`: connect by name alone;
   - a Prometheus-compatible URL: connect it, choosing a short `name`; flavor
     `victoriametrics` only when you know it is VM (check `source_status`: application/version),
     else `prometheus`;
   - a Grafana URL (a bare Grafana host, or a dashboard `/d/...` or `/explore` link: use its
     scheme and host only): the Grafana front door. `source_discover_grafana(url)` lists its
     datasources. Keep the `supported` ones (Prometheus-compatible); if exactly one, use it; if
     several, show them (name, type, `backend_hint`, default first) and ask which one; if none
     is supported, say which types it has and stop. Connect it with
     `source_connect(name=<short name>, grafana=<url>, uid=<uid>)` (the datasource's buildinfo
     picks the flavor: do not pass one) and report the `backend` it returns. If discovery needs
     auth (an empty or 401/403 result), ask the user for a Grafana token in a file and retry
     with `auth_file`.
   Never ask for or pass a token: if auth is needed, ask the user to put it in a file and pass
   `auth_file` (or `auth_env` naming a daemon env var).
3. **Connect.** `source_connect`, then `source_status`. On failure report the error and its hint
   verbatim and ask for a corrected URL; do not retry blindly.
4. **Learn.** `source_learn(source)` once (tiered and bounded; it only lists metric names and
   reads declared metadata, packs and naming conventions). Then `binding_suggest(source)`
   (reads the catalog only, nothing is queried). Do not sweep the catalog or start charting.
   Then `lessons_for(source)`: approved lessons from earlier sessions that apply to the whole
   source (`held` counts those scoped to a service: `/telemetry-nerd:investigate` asks for them
   once the services are known).
5. **Open the UI.** Print the workspace URL for the user to open (/telemetry-nerd:open does the same).
6. **Report briefly:** source name, flavor and reachability; metric count from the learn result;
   the knowledge packs matched (node_exporter, Kubernetes, ...) if the result lists any; how many
   binding suggestions were found (RED / USE / Little's law) and the top ones; caveats
   (`metadata_coverage`, `cardinality_unavailable`, `metrics_truncated`) in one line; the
   lessons that apply, each with its id and evidence ids, as priors to check (not facts).
   Suggest next steps: `/telemetry-nerd:investigate <question>` (e.g. "why did checkout latency spike at
   14:00?"), `/telemetry-nerd:learn` to deepen what the catalog knows, `/telemetry-nerd:open` for the workspace, or call `workspace_list` to resume an old investigation.
