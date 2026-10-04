import { expect, test } from "./fixtures.js";

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
  // d has a 15-minute hole every 3h (anchored at the fixture source's start); e is born mid-history and
  // may ALSO read "no samples" before its birth (correct labelling). Pick d's note by its label
  // instead of relying on which series' note comes first.
  const note = el
    .locator('[data-note^="missing_data:"]')
    .filter({ hasText: 'instance="d"' })
    .filter({ hasText: "no samples" })
    .first();
  await expect(note).toBeVisible();
  await note.hover();
  const box = await el.locator("[data-rug]").boundingBox();
  if (!box) throw new Error("rug not laid out");
  await page.mouse.move(box.x + box.width / 2 - 40, box.y + 4);
  // the hint names the series, a bucket range and a state word (which bucket is under the pointer is
  // layout-dependent, so the state is not pinned), and stays inside the panel's plot area
  const tip = el.locator(".rug-tip");
  await expect(tip).toBeVisible();
  expect(await tip.evaluate((n) => getComputedStyle(n).position)).toBe("absolute");
  await expect(tip).toContainText(/instance="[de]"/);
  await expect(tip).toContainText(/\d{2}:\d{2}–\d{2}:\d{2} · (ok|fewer samples than expected|no samples|series not seen yet)/);
  const plot = await el.locator(".plot").boundingBox();
  const tb = await tip.boundingBox();
  if (!plot || !tb) throw new Error("tip or plot not laid out");
  expect(tb.x).toBeGreaterThanOrEqual(plot.x - 1);
  expect(tb.x + tb.width).toBeLessThanOrEqual(plot.x + plot.width + 1);
  // pointer on the rug's last cell (the canvas spans the axis gutters too; cells span only the
  // uPlot plotting area): the tip stays inside the plot area
  const rug = await el.locator("[data-rug]").boundingBox();
  const over = await el.locator(".u-over").first().boundingBox();
  if (!rug || !over) throw new Error("rug or plotting area not laid out");
  await page.mouse.move(over.x + over.width - 2, rug.y + 4);
  await expect(tip).toBeVisible();
  const edge = await tip.boundingBox();
  if (!edge) throw new Error("tip not laid out");
  expect(edge.x + edge.width).toBeLessThanOrEqual(plot.x + plot.width + 1);
  expect(edge.y + edge.height).toBeLessThanOrEqual(plot.y + plot.height + 1);
});
