import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers";

// Collapse/fold a panel to just its header (bead telemetry-nerd-y5j4): a per-browser UI
// preference (localStorage, not workspace data), so it survives a reload but isn't shared with
// other viewers — unlike close, which is workspace state everyone sees.
test("collapse: folds the chart away, keeps the header, and survives a reload", async ({ page }) => {
  const panel = await seedPanel(page.request, "collapse: does this panel fold?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  const toggle = el.locator("[data-collapse]");
  await toggle.click();
  await expect(el).toHaveAttribute("data-collapsed", "true");
  await expect(el.locator(".plot")).toHaveCount(0);
  // the header stays: id, question and export/close controls are still there
  await expect(el.locator(".obj-id")).toBeVisible();
  await expect(el.locator(".question")).toContainText(panel.question);
  // collapsed: nothing to export, so the buttons are disabled rather than silently no-op-ing
  await expect(el.locator('[data-export="png"]')).toBeDisabled();

  await page.reload();
  const elAfterReload = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(elAfterReload).toHaveAttribute("data-collapsed", "true");
  await expect(elAfterReload.locator(".plot")).toHaveCount(0);

  await elAfterReload.locator("[data-collapse]").click();
  await expect(elAfterReload).toHaveAttribute("data-collapsed", "false");
  await expect(elAfterReload.locator("canvas").first()).toBeVisible();
});
