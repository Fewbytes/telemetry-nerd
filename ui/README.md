# Telemetry Nerd UI

Svelte 5 + TypeScript + uPlot. Dev commands (run from the repo root):

- `just ui-install` / `just ui-dev` (Vite, proxies /api and /ws to 127.0.0.1:7070)
- `just ui-test` (vitest), `just ui-check` (svelte-check), `just ui-build`
- `just e2e` (Playwright against the built UI and a live daemon)
