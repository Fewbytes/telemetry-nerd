import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
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