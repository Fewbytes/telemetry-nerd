import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers";

test("simple mode: one panel can peek at its nerd view without changing the global mode", async ({ page }) => {
  const a = await seedPanel(page.request, "peek: panel A");
  const b = await seedPanel(page.request, "peek: panel B");
  await page.goto("/");
  const elA = page.locator(`[data-panel-id="${a.id}"]`);
  const elB = page.locator(`[data-panel-id="${b.id}"]`);
  await expect(elA.locator("canvas").first()).toBeVisible();
  await expect(elB.locator("canvas").first()).toBeVisible();
  await page.locator("[data-mode-toggle=simple]").click();

  for (const el of [elA, elB]) {
    await expect(el.locator("[data-simple-toolbar]")).toBeVisible();
    await expect(el.locator(".obj-id")).toHaveCount(0);
  }

  await elA.getByRole("button", { name: "Panel actions" }).click();
  await elA.locator("[data-peek=nerd]").click();

  await expect(elA.locator(".obj-id")).toBeVisible();
  await expect(elA.locator(".shown")).toBeVisible();
  await expect(elA.locator("[data-metric-card]").first()).toBeVisible();
  await expect(elA.locator(".legend.time-range")).toBeVisible();
  await expect(elA.locator("[data-simple-toolbar]")).toHaveCount(0);
  await expect(elA.locator("[data-peek=simple]")).toBeVisible();
  await expect(elA.locator("[data-peek=simple]")).toBeFocused();
  // the other panel and the global mode are untouched
  await expect(elB.locator("[data-simple-toolbar]")).toBeVisible();
  await expect(elB.locator(".obj-id")).toHaveCount(0);
  await expect(elB.locator("[data-peek=simple]")).toHaveCount(0);
  await expect(page.locator("html")).toHaveAttribute("data-mode", "simple");

  await elA.locator("[data-peek=simple]").click();
  await expect(elA.locator("[data-simple-toolbar]")).toBeVisible();
  await expect(elA.locator(".obj-id")).toHaveCount(0);
  await expect(elA.locator("[data-peek=simple]")).toHaveCount(0);
  await expect(elA.getByRole("button", { name: "Panel actions" })).toBeFocused();

  // session-only: a reload forgets the peek
  await elA.getByRole("button", { name: "Panel actions" }).click();
  await elA.locator("[data-peek=nerd]").click();
  await expect(elA.locator(".obj-id")).toBeVisible();
  await page.reload();
  await expect(elA.locator("canvas").first()).toBeVisible();
  await expect(elA.locator("[data-simple-toolbar]")).toBeVisible();
  await expect(elA.locator(".obj-id")).toHaveCount(0);
});

test("simple mode: peeking keeps the preview chart, and nerd mode has no peek buttons", async ({ page }) => {
  const a = await seedPanel(page.request, "peek: preview and nerd identity");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${a.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();
  await page.locator("[data-mode-toggle=simple]").click();
  await el.locator("[data-simple-toolbar]").getByRole("button", { name: "6h", exact: true }).click();
  await expect(el.locator("[data-preview-render] .u-over")).toBeVisible();

  await el.getByRole("button", { name: "Panel actions" }).click();
  await el.locator("[data-peek=nerd]").click();
  await expect(el.locator("[data-peek=simple]")).toBeVisible();
  await expect(el.locator("[data-preview-render] .u-over")).toBeVisible();
  await expect(el.locator("[data-preview-render]")).toHaveCount(1);

  // the peek survives a global toggle but only shows in simple mode; nerd DOM carries no peek buttons
  await page.locator("[data-mode-toggle=nerd]").click();
  await expect(page.locator("[data-peek]")).toHaveCount(0);
  await page.locator("[data-mode-toggle=simple]").click();
  await expect(el.locator("[data-peek=simple]")).toBeVisible();
});
