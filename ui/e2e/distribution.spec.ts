import { expect, test } from "@playwright/test";
import { brushMiddleThird } from "./helpers.js";

test("histogram heatmap within budget; brush opens the distribution with n", async ({ page, request }) => {
  const q = await request.post("/api/query-distribution", {
    data: { selector: "tn_demo_request_duration_seconds_bucket", by: ["instance"], start: "now-3h", end: "now-10m" },
  });
  expect(q.ok()).toBeTruthy();
  const { dataset, summary } = await q.json();
  expect(summary.representation).toBe("distribution");
  const s = await request.post("/api/show", { data: { dataset, question: "How is demo latency distributed over time?", unit: "s" } });
  const { panel } = await s.json();
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas.heatmap")).toHaveCount(3);
  await expect(el).toHaveAttribute("data-budget-exceeded", "false");

  await brushMiddleThird(page, panel.id);
  await page.locator(".selection-menu").getByRole("button", { name: "Distribution here" }).click();
  const hist = page.locator("section.panel", { hasText: "distributed between" });
  await expect(hist.locator("canvas.distribution").first()).toBeVisible();
  await expect(hist.locator(".legend").first()).toContainText("n =");
  await expect(hist.locator(".legend").first()).toContainText("previous");
});

test("percentile bands view and CCDF view render from the same payloads", async ({ page, request }) => {
  const q = await request.post("/api/query-distribution", {
    data: { selector: "tn_demo_request_duration_seconds_bucket", by: ["instance"], start: "now-3h", end: "now-10m" },
  });
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question: "How did demo latency percentiles move?", unit: "s" } });
  const { panel } = await s.json();
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await el.getByRole("button", { name: "percentile bands" }).click();
  await expect(el.locator("canvas.percentiles")).toHaveCount(3);
  await expect(el).not.toHaveAttribute("data-percentile-bands", "0");
  await el.getByRole("button", { name: "p99.9", exact: true }).click();
  await expect(el.locator(".hint")).toContainText("n ≥ 10/(1−q)");
});
