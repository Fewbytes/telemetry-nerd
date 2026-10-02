import { expect, test } from "@playwright/test";

test("a series with a hole shows a coverage rug and a located caveat", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: "tn_demo_holes_seconds", start: "now-6h", end: "now-10m", step: "1m" },
  });
  expect(q.ok()).toBeTruthy();
  const { dataset, summary } = await q.json();
  expect(summary.caveats).toContain("missing_data");
  const s = await request.post("/api/show", { data: { dataset, question: "Where is data missing?" } });
  const { panel } = await s.json();
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("[data-rug]")).toBeVisible();
  // instance d has the empty hole; instance e (late born) only has a partial first bucket
  const note = el.locator('[data-note^="missing_data:"]').filter({ hasText: "no samples" }).first();
  await expect(note).toContainText('instance="d"');
  await note.hover();
  const box = await el.locator("[data-rug]").boundingBox();
  if (!box) throw new Error("rug not laid out");
  await page.mouse.move(box.x + box.width / 2 - 40, box.y + 4);
  // the hint names the series, a bucket range and a state word (which bucket is under the pointer is
  // layout-dependent, so the state is not pinned), and stays inside the panel's plot area
  const tip = el.locator(".rug-tip");
  await expect(tip).toBeVisible();
  await expect(tip).toContainText(/instance="[de]"/);
  await expect(tip).toContainText(/\d{2}:\d{2}–\d{2}:\d{2} · (ok|fewer samples than expected|no samples|series not seen yet)/);
  const plot = await el.locator(".plot").boundingBox();
  const tb = await tip.boundingBox();
  if (!plot || !tb) throw new Error("tip or plot not laid out");
  expect(tb.x).toBeGreaterThanOrEqual(plot.x - 1);
  expect(tb.x + tb.width).toBeLessThanOrEqual(plot.x + plot.width + 1);
});
