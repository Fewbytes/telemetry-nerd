import { expect, test } from "@playwright/test";

test("query → show → panel renders envelope within budget, peak preserved", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: "tn_demo_latency_seconds", start: "now-3h", end: "now-10m", step: "1m" },
  });
  expect(q.ok()).toBeTruthy();
  const { dataset, summary } = await q.json();
  expect(summary.series_count).toBe(3);

  const question = "Is instance c's latency spike visible without peak erosion?";
  const s = await request.post("/api/show", { data: { dataset, question } });
  expect(s.ok()).toBeTruthy();
  const { panel } = await s.json();

  // Heavy downsampling (≈170 buckets → 50px) must keep the 1.5s spike in the max envelope.
  const d = await (await request.get(`/api/panels/${panel.id}/data?width=50`)).json();
  const peak = Math.max(...d.series.flatMap((x: { max: (number | null)[] }) => x.max.filter((v) => v !== null)));
  expect(peak).toBeGreaterThanOrEqual(1.5);

  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.getByText(question)).toBeVisible();
  await expect(el.locator("canvas").first()).toBeVisible();
  // 2as.10: either the honest "no reference range yet" note, or, once the operating profile has
  // arrived, the note saying what the reference range includes
  await expect(
    el.locator('[data-note="y_scaled_to_data"], [data-note="y_reference"]').first(),
  ).toBeVisible();
  await expect(el).toHaveAttribute("data-budget-exceeded", "false");
});

test("a panel without a question is rejected", async ({ request }) => {
  const q = await request.post("/api/query", { data: { expr: "tn_demo_latency_seconds", start: "now-1h", end: "now-10m" } });
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question: "" } });
  expect(s.status()).toBe(400);
});