import { test, expect } from "./fixtures.js";

// The fixtures `page` pins tn-mode-default=nerd on every navigation while the key is absent, so
// this test builds its own context: nothing is pinned and the real default is observed.
test("default mode is changed in settings and survives a reload", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL });
  try {
    const page = await context.newPage();
    await page.goto("/");
    await expect(page.locator("html")).toHaveAttribute("data-mode", "simple");

    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByLabel("Default mode").selectOption("nerd");
    await expect(page.locator("html")).toHaveAttribute("data-mode", "nerd");

    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-mode", "nerd");
  } finally {
    await context.close();
  }
});

test("header toggle overrides for the session only", async ({ page }) => {
  await page.goto("/");
  await page.locator("[data-mode-toggle=simple]").click();
  await expect(page.locator("html")).toHaveAttribute("data-mode", "simple");
  expect(await page.evaluate(() => localStorage.getItem("tn-mode-default"))).toBe("nerd");
});
