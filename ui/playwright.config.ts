import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  // serialize: tests share one daemon (channel claim cursors, channel state)
  // and global UI state (theme flips rebuild every plot on every page)
  workers: 1,
  globalSetup: "./e2e/global-setup.ts",
  use: { baseURL: "http://127.0.0.1:7071" },
  webServer: {
    command:
      'uv run --directory .. telemetry-nerd serve --port 7071 --data-dir "$(mktemp -d)" --source-url http://127.0.0.1:8428',
    url: "http://127.0.0.1:7071/api/panels",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});