import { defineConfig } from "@playwright/test";

// Ports are overridable so parallel sessions can run e2e on random free ports:
// E2E_PORT (daemon, default 7071) and E2E_FIXTURE_PORT (fixture source, default E2E_PORT + 1).
const port = Number(process.env.E2E_PORT ?? 7071);
const daemon = `http://127.0.0.1:${port}`;
// The daemon's only source is the fixture PromQL server (bead y7hb): seeded in-process with the
// synthetic demo series anchored at its start, no VictoriaMetrics, no podman. Specs add their own
// series through its import endpoint (helpers.FIXTURE).
// The fixture is a Prometheus-flavour engine, so e2e exercises the daemon's Prometheus paths.
// VictoriaMetrics paths (rollup, nocache, the VM missing-data profile, keep_metric_names, tiled
// littles) are covered by recorded-response unit tests and tests/integration.
const fixturePort = Number(process.env.E2E_FIXTURE_PORT ?? port + 1);
const fixture = `http://127.0.0.1:${fixturePort}`;

export default defineConfig({
  testDir: "e2e",
  // serialize: tests share one daemon (channel claim cursors, channel state)
  // and global UI state (theme flips rebuild every plot on every page)
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  use: { baseURL: daemon },
  webServer: [
    {
      command: `uv run --directory .. python -m telemetry_nerd.devtools.promfixture --port ${fixturePort}`,
      url: `${fixture}/api/v1/status/buildinfo`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command:
        `uv run --directory .. telemetry-nerd serve --port ${port} --data-dir "$(mktemp -d)" ` +
        `--source-url ${fixture} --source-flavor prometheus`,
      url: `${daemon}/api/panels`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
