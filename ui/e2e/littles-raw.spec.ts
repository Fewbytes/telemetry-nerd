import { expect, importSeries, test } from "./fixtures.js";
import { FIXTURE, mcpTool } from "./helpers";

// The spec's own series: a synthetic Little's law triple (arrivals counter, latency _sum/_count,
// concurrency gauge) with a load peak near the end, so both the verdict view (a promoted/transient
// window) and the raw validation view (a visible peak) have something to show.
const PREFIX = `tn_e2e_littles_${Date.now()}`;
const STEP = 15_000;
const N = 240; // 1h at 15s
const END = Math.floor((Date.now() - 5 * 60_000) / STEP) * STEP;
const START = END - (N - 1) * STEP;
const PEAK = [160, 190] as const; // sub-steps with a load peak (rate and latency both rise)

test.beforeAll(async ({ request }) => {
  const lines: string[] = [];
  let arrivals = 0, latSum = 0, latCount = 0;
  for (let i = 0; i < N; i++) {
    const t = START + i * STEP;
    const peak = i >= PEAK[0] && i < PEAK[1];
    const rate = 20 + 8 * Math.sin(i / 25) + (peak ? 25 : 0);
    const w = 0.2 + (peak ? 0.35 : 0);
    const reqs = (rate * STEP) / 1000;
    arrivals += reqs;
    latSum += reqs * w;
    latCount += reqs;
    const conc = Math.max(0, rate * w + (Math.random() - 0.5) * 2);
    lines.push(`${PREFIX}_arrivals_total ${arrivals.toFixed(3)} ${t}`);
    lines.push(`${PREFIX}_latency_seconds_sum ${latSum.toFixed(3)} ${t}`);
    lines.push(`${PREFIX}_latency_seconds_count ${latCount.toFixed(3)} ${t}`);
    lines.push(`${PREFIX}_concurrency ${conc.toFixed(3)} ${t}`);
  }
  await importSeries(request, lines.join("\n") + "\n");
});

test.afterAll(async ({ request }) => {
  await request.post(`${FIXTURE}/api/v1/admin/tsdb/delete_series?match[]={__name__=~"${PREFIX}.*"}`);
});

test("littles panel: raw validation view toggles alongside the verdict view", async ({ page, request }) => {
  const check = await mcpTool(request, "check_littles_law", {
    arrival_rate: `${PREFIX}_arrivals_total`,
    latency: `${PREFIX}_latency_seconds`,
    concurrency: `${PREFIX}_concurrency`,
    start: String(START),
    end: String(END),
    window: "2m",
    detail: true,
  });
  const dsId = check.datasets.concurrency;
  const shown = await mcpTool(request, "show", { dataset: dsId, question: "is L = lambda W?", mark: "littles" });
  const id = shown.panel;

  await page.goto("/");
  const panel = page.locator(`[data-panel-id="${id}"]`);
  const littles = panel.locator(".littles").first();
  await expect(littles).toBeVisible();
  await expect(littles).toHaveAttribute("data-littles-view", "verdict");

  await page.screenshot({ path: "test-results/littles-verdict-view.png" });

  await littles.locator("[data-littles-view-raw]").click();
  await expect(littles).toHaveAttribute("data-littles-view", "raw");
  await expect(littles.locator("[data-littles-raw] > div canvas")).toHaveCount(3);

  await page.screenshot({ path: "test-results/littles-raw-view.png" });
});
