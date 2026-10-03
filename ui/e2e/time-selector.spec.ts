import { expect, test } from "@playwright/test";
import { seedPanel } from "./helpers.js";

test("preset range change previews, then keep-this-range creates a new panel", async ({ page, request }) => {
  const panel = await seedPanel(request, "Does demo latency drift within its normal band?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const row = el.getByRole("group", { name: "Time range" });
  const panelCountBefore = await page.locator("[data-panel-id]").count();

  await row.getByRole("button", { name: "6h" }).click();
  await expect(el.locator("[data-preview-badge]")).toBeVisible();

  await row.getByRole("button", { name: "keep this range" }).click();
  await expect(page.locator("[data-panel-id]")).toHaveCount(panelCountBefore + 1);
});
