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
    await page.getByRole("radio", { name: "nerd" }).check();
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

test("settings dialog: focus moves in on open, Esc returns it to the trigger", async ({ page }) => {
  await page.goto("/");
  const trigger = page.getByRole("button", { name: "Settings", exact: true });
  await expect(trigger).toHaveAttribute("aria-haspopup", "dialog");
  await trigger.click();
  await expect(page.getByRole("dialog", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("radio", { name: "nerd" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Settings" })).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("re-choosing the current default clears the session override", async ({ page }) => {
  await page.goto("/"); // fixtures pin the default to nerd
  await page.locator("[data-mode-toggle=simple]").click();
  await expect(page.locator("html")).toHaveAttribute("data-mode", "simple");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("radio", { name: "nerd" }).click(); // already the default
  await expect(page.locator("html")).toHaveAttribute("data-mode", "nerd");
  expect(await page.evaluate(() => sessionStorage.getItem("tn-mode"))).toBeNull();
});

// qbd4: the header holds a mode toggle, workspace switcher, PDF export, connection pill, Settings,
// build id and theme; on narrow windows the controls wrap instead of overflowing the page.
test("header controls wrap without horizontal overflow at narrow widths", async ({ page }) => {
  for (const width of [360, 600, 800]) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/");
    const settings = page.getByRole("button", { name: "Settings", exact: true });
    await expect(settings).toBeVisible();
    const fits = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
    expect(await fits()).toBe(true);
    for (const el of [page.locator("[data-mode-toggle=simple]"), page.locator("[data-mode-toggle=nerd]"), settings, page.getByLabel("Theme")]) {
      const box = await el.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    }
    await settings.click(); // the open dialog stays on screen too
    const pop = await page.getByRole("dialog", { name: "Settings" }).boundingBox();
    expect(pop!.x).toBeGreaterThanOrEqual(0);
    expect(pop!.x + pop!.width).toBeLessThanOrEqual(width);
    expect(await fits()).toBe(true);
    await page.locator("[data-mode-toggle=simple]").click(); // reachable: a click lands
    await expect(page.locator("html")).toHaveAttribute("data-mode", "simple");
  }
});
