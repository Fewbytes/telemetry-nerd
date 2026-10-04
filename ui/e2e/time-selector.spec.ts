import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers.js";

test("preset range change previews, then keep-this-range creates a new panel", async ({ page, request }) => {
  const panel = await seedPanel(request, "Does demo latency drift within its normal band?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const row = el.getByRole("group", { name: "Time range" });
  const panelCountBefore = await page.locator("[data-panel-id]").count();

  await row.getByRole("button", { name: "6h" }).click();
  const badge = el.locator("[data-preview-badge]");
  await expect(badge).toBeVisible();
  // honest copy: the chart does not re-render the fetched range yet (deferred), so never "previewing"
  await expect(badge).toContainText("fetched");
  await expect(badge).toContainText("keep this range");
  await expect(badge).not.toContainText("previewing");

  await row.getByRole("button", { name: "keep this range" }).click();
  await expect(page.locator("[data-panel-id]")).toHaveCount(panelCountBefore + 1);
});
