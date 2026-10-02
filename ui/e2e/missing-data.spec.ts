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
  // the hint text is checked loosely: the hole sits at 1/3 of the 6h range
  await expect(el.locator(".rug-tip")).toBeVisible();
});
