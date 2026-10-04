import { expect, test } from "./fixtures.js";
import { brushMiddleThird, seedPanel } from "./helpers.js";

// Theme toggle: explicit choice applies immediately, survives reload
// (localStorage + no-flash bootstrap script), and "system" follows the OS.
test("theme toggle switches, persists, and resets to system", async ({ page }) => {
  await page.goto("/");
  const html = page.locator("html");
  const toggle = page.getByLabel("Theme");

  await toggle.selectOption("dark");
  await expect(html).toHaveAttribute("data-theme", "dark");

  await toggle.selectOption("light");
  await expect(html).toHaveAttribute("data-theme", "light");

  // choice persists across reload
  await toggle.selectOption("dark");
  await page.reload();
  await expect(html).toHaveAttribute("data-theme", "dark");

  await toggle.selectOption("system");
  await page.reload();
  const stored = await page.evaluate(() => localStorage.getItem("tn-theme"));
  expect(stored).toBe("system");
});

test("⌘/Ctrl+Enter sends from the Ask-Claude box", async ({ page, request }) => {
  const panel = await seedPanel(request, "Does the keyboard shortcut send the question?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  await brushMiddleThird(page, panel.id);
  const menu = page.locator(".selection-menu");
  await expect(menu).toBeVisible();
  await menu.getByRole("button", { name: "Ask Claude…" }).click();
  const textarea = menu.getByPlaceholder("Ask Claude about this selection");
  await textarea.fill("e2e keyboard send");
  await textarea.press("ControlOrMeta+Enter");
  await expect(menu).toBeHidden();

  // the thread renders under the panel with the user's message
  const thread = el.locator(".thread");
  await expect(thread).toBeVisible();
  await expect(thread.locator(".message .text")).toContainText("e2e keyboard send");

  // exactly once: one claim returns it, the next finds nothing new
  const claim = await (
    await request.post("/api/channel/claim", { data: { consumer: "e2e-kbd" } })
  ).json();
  expect(claim.content).toContain("e2e keyboard send");
  const again = await (
    await request.post("/api/channel/claim", { data: { consumer: "e2e-kbd" } })
  ).json();
  expect(again.content).toBeNull();
});