import { expect, test } from "@playwright/test";

test("a raw counter is charted as its rate, and the panel says so", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: "tn_demo_requests_total", start: "now-3h", end: "now-10m", step: "1m" },
  });
  const { dataset } = await q.json();
  const shown = await request.post("/api/show", { data: { dataset, question: "How busy is the demo service?" } });
  const panel = (await shown.json()).panel;
  expect(panel.spec.auto.transform).toBe("rate");
  expect(panel.dataset_ids[0]).not.toBe(dataset);

  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator('[data-note="auto_rate"]')).toContainText("Shown as a rate");
  await expect(el.locator('[data-note="auto_rate"]')).toContainText(`dataset ${dataset}`);
  await expect(el).toContainText("rate(tn_demo_requests_total[");

  // asking for the running total explicitly draws it, with no transform note
  const raw = await request.post("/api/show", { data: { dataset, question: "The running total?", raw: true } });
  const rawPanel = (await raw.json()).panel;
  await page.reload();
  const rawEl = page.locator(`[data-panel-id="${rawPanel.id}"]`);
  await expect(rawEl.locator("canvas").first()).toBeVisible();
  await expect(rawEl.locator('[data-note="auto_rate"]')).toHaveCount(0);
});
