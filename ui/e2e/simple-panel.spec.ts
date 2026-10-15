import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers";

test("simple mode: minimal chrome, essentials kept, nerd restored by the toggle", async ({ page }) => {
  const panel = await seedPanel(page.request, "simple: is the chrome minimal?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  // nerd (fixture pin): the parts exist
  await expect(el.locator(".obj-id").first()).toBeVisible();
  await expect(el.locator(".shown")).toBeVisible();
  await expect(el.locator("[data-metric-card]")).toHaveCount(1);
  await expect(el.locator("[data-export=png]")).toBeVisible();

  await page.locator("[data-mode-toggle=simple]").click();
  await expect(page.locator("html")).toHaveAttribute("data-mode", "simple");

  // kept
  await expect(el.locator(".question")).toContainText(panel.question);
  await expect(el.locator("[data-refresh]")).toBeVisible();
  await expect(el.locator("canvas").first()).toBeVisible();
  await expect(el.locator("[data-simple-meta]")).toContainText("step");
  // hidden
  await expect(el.locator(".obj-id")).toHaveCount(0);
  await expect(el.locator(".shown")).toHaveCount(0);
  await expect(el.locator("details.query")).toHaveCount(0);
  await expect(el.locator("[data-metric-card]")).toHaveCount(0);
  await expect(el.locator(".resize-handle")).toHaveCount(0);
  await expect(el.locator(".export-group")).toHaveCount(0);
  await expect(el.locator("[data-export=png]")).toHaveCount(0); // inside the closed menu

  // exports live in the overflow menu
  await el.getByRole("button", { name: "Panel actions" }).click();
  await expect(el.locator("[data-export=png]")).toBeVisible();
  await expect(el.locator("[data-export=pdf]")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(el.locator("[data-export=png]")).toHaveCount(0);

  await page.locator("[data-mode-toggle=nerd]").click();
  await expect(el.locator(".shown")).toBeVisible();
});
