import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers.js";

// The legend names a series by what tells it apart (a, b, c), not by the Prometheus selector;
// the full selector stays on hover (1a8w).
test("legend rows use short names and keep the full selector as a title", async ({ page, request }) => {
  const panel = await seedPanel(request, "Which instance is slow?");
  await page.goto("/");
  const legend = page.locator(`[data-panel-id="${panel.id}"] .u-legend`);
  await expect(legend).toBeVisible();
  const text = await legend.innerText();
  expect(text).not.toContain("tn_demo_latency_seconds{");
  expect(text).not.toContain('instance="');
  const labels = legend.locator(".u-series .u-label");
  for (const n of ["a", "b", "c"]) await expect(labels.filter({ hasText: new RegExp(`^${n}$`) })).toHaveCount(1);
  expect(await legend.locator('.u-series[title*="instance=\\"a\\""]').count()).toBeGreaterThan(0);
});
