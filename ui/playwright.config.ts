import { defineConfig } from "@playwright/test";

// Daemon port is overridable (E2E_PORT) so parallel sessions can run e2e on
// random free ports; defaults to the historical 7071.
const port = Number(process.env.E2E_PORT ?? 7071);
const daemon = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "e2e",
  // serialize: tests share one daemon (channel claim cursors, channel state)
  // and global UI state (theme flips rebuild every plot on every page)
  workers: 1,
  globalSetup: "./e2e/global-setup.ts",
  use: { baseURL: daemon },
  webServer: {
    command: `uv run --directory .. telemetry-nerd serve --port ${port} --data-dir "$(mktemp -d)" --source-url http://127.0.0.1:8428`,
    url: `${daemon}/api/panels`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
