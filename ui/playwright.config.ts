import { chromium, defineConfig, type Page } from "@playwright/test";

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

// CPU-throttle stress (bead zek0.1): E2E_CPU_THROTTLE=<rate> (e.g. 4 = 4x slower) applies CDP
// Emulation.setCPUThrottlingRate to every page of every context, to expose timing assumptions
// that a fast machine hides. Off by default. Specs import `test` from @playwright/test directly,
// so this wraps the chromium launcher once per process (the config is also loaded by workers)
// instead of adding a fixture they would each have to opt into.
const throttle = Number(process.env.E2E_CPU_THROTTLE ?? 0);
if (throttle > 1) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const proto: any = Object.getPrototypeOf(chromium);
  const launch = proto.launch;
  proto.launch = async function (...args: unknown[]) {
    const browser = await launch.apply(this, args);
    const newContext = browser.newContext.bind(browser);
    browser.newContext = async (...a: unknown[]) => {
      const ctx = await newContext(...a);
      const apply = async (page: Page) => {
        const cdp = await ctx.newCDPSession(page);
        await cdp.send("Emulation.setCPUThrottlingRate", { rate: throttle });
      };
      ctx.on("page", (p: Page) => void apply(p));
      return ctx;
    };
    return browser;
  };
}

export default defineConfig({
  testDir: "e2e",
  // serialize: tests share one daemon, and it has one active workspace. Each test runs in a fresh
  // workspace of its own (e2e/fixtures.ts) and writes global state (catalog, lessons, channel
  // cursors) only under its own names, so any order and any --repeat-each pass (zek0.2).
  workers: 1,
  // A pass-on-retry is a FAILURE (zek0 policy: a flake is a P0 bug). CI keeps one retry only so
  // the failing run records both attempts and a trace (trace: on-first-retry); failOnFlakyTests
  // makes the run red regardless of whether the retry passed. Locally: no retry, no trace noise.
  retries: process.env.CI ? 1 : 0,
  failOnFlakyTests: true,
  use: { baseURL: daemon, trace: process.env.CI ? "on-first-retry" : "off" },
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
