import { expect, importSeries, test } from "./fixtures.js";
import { FIXTURE, mcpTool, seedPanel } from "./helpers.js";

// Nerd mode's control rows (NerdControls), each in an end state: the data filter, Claude's
// y-view suggestions, the split-by-outcome note and the time-range error. The fixtures `page`
// pins nerd mode, so no toggle is clicked.

test("data filter row: views, Claude's pick marked, filter-edge caveat; a click switches the view", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: "tn_demo_latency_seconds", start: "now-3h", end: "now-10m", step: "1m" },
  });
  expect(q.ok()).toBeTruthy();
  const { dataset } = await q.json();
  const f = await mcpTool(request, "filter", { dataset, kind: "lowpass", period: "15m", reason: "the trend under the jitter" });
  const { panel } = await mcpTool(request, "show", { dataset: f.dataset, question: "nerd: the data filter row" });
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel}"]`);

  const row = el.getByRole("group", { name: "Data view" });
  await expect(row).toContainText("dashed: filter edge (unreliable)");
  const claude = row.locator("button", { hasText: "· Claude" });
  await expect(claude).toHaveAttribute("title", "Claude's pick: the trend under the jitter");
  await expect(claude).toHaveClass(/on/);
  const raw = row.locator("[data-view=raw]");
  await raw.click();
  await expect(raw).toHaveClass(/on/);
  await expect(claude).not.toHaveClass(/on/);
});

test("Claude's y-view suggestions follow 'Claude:' in the y row and apply on click", async ({ page, request }) => {
  const panel = await seedPanel(request, "nerd: do Claude's y views show?");
  await mcpTool(request, "suggest_y_view", {
    panel: panel.id, mode: "data", label: "fit the wiggles", reason: "the normal range flattens the series",
  });
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  const row = el.getByRole("group", { name: "Y-axis view" });
  await expect(row).toContainText("· Claude:");
  const s = row.locator("[data-y-suggestion]");
  await expect(s).toHaveText("fit the wiggles");
  await expect(s).toHaveAttribute("title", "the normal range flattens the series");
  await expect(s).not.toHaveClass(/on/);
  await s.click();
  await expect(s).toHaveClass(/on/);
  await expect(el.locator('[data-note="y_view"]')).toContainText("fit the wiggles");
});

test("a time range the box cannot parse shows the range error, and a valid one clears it", async ({ page, request }) => {
  const panel = await seedPanel(request, "nerd: range error");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  const row = el.getByRole("group", { name: "Time range" });
  const box = row.getByPlaceholder("now-3h");
  await box.fill("last tuesday-ish");
  await box.press("Enter");
  await expect(row.locator("[data-range-error]")).toHaveText('could not parse "last tuesday-ish"');
  await expect(row.locator("[data-preview-badge]")).toHaveCount(0);

  await row.getByRole("button", { name: "6h", exact: true }).click();
  await expect(row.locator("[data-range-error]")).toHaveCount(0);
  await expect(row.locator("[data-preview-badge]")).toBeVisible();
});

test("split by success / failure: the follow-up note names the outcome label and the new panels", async ({ page, request, uniq }) => {
  const metric = `tn_e2e_${uniq}_dur_seconds_bucket`;
  const step = 30_000;
  const end = Math.floor((Date.now() - 5 * 60_000) / step) * step;
  const les = ["0.1", "0.5", "1", "+Inf"];
  const lines = [`# TYPE ${metric.replace("_bucket", "")} histogram`];
  const total: Record<string, number> = { "200": 0, "500": 0 };
  for (let i = 0; i < 360; i++) {
    const t = end - (359 - i) * step;
    for (const code of ["200", "500"]) {
      total[code] += code === "200" ? 100 : 5;
      // successes are fast, failures slow
      const share = code === "200" ? [0.7, 0.95, 0.99, 1] : [0.05, 0.3, 0.6, 1];
      les.forEach((le, k) => lines.push(`${metric}{code="${code}",le="${le}"} ${(total[code] * share[k]).toFixed(1)} ${t}`));
    }
  }
  try {
    await importSeries(request, lines.join("\n") + "\n");
    const q = await request.post("/api/query-distribution", {
      data: { selector: metric, by: ["code"], start: "now-3h", end: "now-10m" },
    });
    expect(q.ok(), await q.text()).toBeTruthy();
    const { dataset } = await q.json();
    const { panel } = await mcpTool(request, "show", { dataset, question: "nerd: split by outcome", unit: "s" });
    await page.goto("/");
    const el = page.locator(`[data-panel-id="${panel}"]`);
    await expect(el.getByRole("group", { name: "Follow-ups" })).toBeVisible();

    const split = el.locator("[data-follow-up=split-outcome]");
    await expect(el.locator("[data-split-note]")).toHaveCount(0);
    await split.click();
    const note = el.locator("[data-split-note]");
    await expect(note).toContainText("by code: successes p");
    await expect(note).toContainText("failures p");
    await expect(split).toBeEnabled();
  } finally {
    await request.post(`${FIXTURE}/api/v1/admin/tsdb/delete_series?match[]={__name__=~"tn_e2e_${uniq}.*"}`);
  }
});
