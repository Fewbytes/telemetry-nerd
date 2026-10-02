import { expect, test } from "@playwright/test";

// Seasonal panel (lkn.9): overlay of now vs previous days with the band, and the ratio view.
// Seeds its own uniquely named metric into the dev VictoriaMetrics (8 days at 5m: a daily cycle,
// one series boosted x2.5 over the last 3h) and deletes it afterwards.
const VM = "http://127.0.0.1:8428";
const METRIC = `tn_e2e_seasonal_${Date.now()}_${Math.floor(Math.random() * 1e6)}`;
const STEP = 300_000;

function load(t: number, rnd: () => number): number {
  const hour = (t % 86_400_000) / 3_600_000;
  const daily = 1 + 2 * Math.exp(-(((hour - 14) / 3.5) ** 2));
  const u = Math.max(1e-9, rnd()), v = rnd();
  const z = Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); // Box-Muller
  return 100 * daily * Math.exp(0.05 * z);
}

test.beforeAll(async ({ request }) => {
  let seed = 12345;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
  const end = Math.floor(Date.now() / STEP) * STEP;
  const lines: string[] = [];
  for (let t = end - 8 * 86_400_000; t <= end; t += STEP) {
    const boost = t > end - 3 * 3_600_000 ? 2.5 : 1;
    lines.push(`${METRIC}{case="normal"} ${load(t, rnd).toFixed(3)} ${t}`);
    lines.push(`${METRIC}{case="boost"} ${(load(t, rnd) * boost).toFixed(3)} ${t}`);
  }
  const r = await request.post(`${VM}/api/v1/import/prometheus`, { data: lines.join("\n") + "\n" });
  expect(r.ok(), "is VictoriaMetrics up on :8428? (just dev-up)").toBeTruthy();
  expect((await request.get(`${VM}/internal/force_flush`)).ok()).toBeTruthy();
});

test.afterAll(async ({ request }) => {
  await request.post(`${VM}/api/v1/admin/tsdb/delete_series?match[]=${METRIC}`);
});

test("seasonal panel: verdicts, overlay and ratio views, dark mode", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: `sum by (case) (${METRIC})`, start: "now-12h", end: "now-5m", step: "5m" },
  });
  expect(q.ok(), await q.text()).toBeTruthy();
  const { dataset } = await q.json();
  const c = await request.post("/api/compare-seasonal", { data: { dataset, cycles: ["1d"] } });
  expect(c.ok(), await c.text()).toBeTruthy();
  const cmp = await c.json();
  const byCase = Object.fromEntries(cmp.series.map((s: { labels: { case: string } }) => [s.labels.case, s]));
  expect(byCase.normal.verdict).toBe("usual");
  expect(byCase.boost.verdict).toBe("unusual");
  expect(byCase.boost.direction).toBe("higher");
  expect(byCase.boost.reference.cycles).toHaveLength(7);

  const s = await request.post("/api/show", {
    data: { dataset, question: "Is now unusual for this time of day?", mark: "seasonal" },
  });
  expect(s.ok(), await s.text()).toBeTruthy();
  const { panel } = await s.json();
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const plots = el.locator(".seasonal");
  await expect(plots).toHaveCount(2);
  const boost = plots.filter({ hasText: 'case="boost"' });
  const normal = plots.filter({ hasText: 'case="normal"' });
  await expect(boost).toHaveAttribute("data-seasonal-verdict", "unusual");
  await expect(normal).toHaveAttribute("data-seasonal-verdict", "usual");
  await expect(boost).toHaveAttribute("data-seasonal-view", "overlay");
  await expect(boost.locator("canvas").first()).toBeVisible();
  await expect(boost.locator(".legend")).toContainText("previous 7 days");
  await expect(boost.locator(".reasons")).toBeVisible();

  // ratio view: same panel, now ÷ reference on a log axis; and back
  await boost.getByRole("button", { name: "ratio" }).click();
  await expect(boost).toHaveAttribute("data-seasonal-view", "ratio");
  await expect(normal).toHaveAttribute("data-seasonal-view", "overlay");
  await expect(boost.locator("canvas").first()).toBeVisible();
  await boost.getByRole("button", { name: "cycles" }).click();
  await expect(boost).toHaveAttribute("data-seasonal-view", "overlay");

  // dark mode: the plots are rebuilt with the dark palette; keep a screenshot to eyeball
  await page.getByLabel("Theme").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await boost.getByRole("button", { name: "ratio" }).click();
  await expect(boost.locator("canvas").first()).toBeVisible();
  await el.screenshot({ path: test.info().outputPath("seasonal-dark-ratio.png") });
  await boost.getByRole("button", { name: "cycles" }).click();
  await el.screenshot({ path: test.info().outputPath("seasonal-dark-overlay.png") });
  await page.getByLabel("Theme").selectOption("light");
  await el.screenshot({ path: test.info().outputPath("seasonal-light-overlay.png") });
  await page.getByLabel("Theme").selectOption("system");
});
