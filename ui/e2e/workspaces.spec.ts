import { expect, test } from "@playwright/test";
import { seedPanel } from "./helpers.js";

test("workspace switcher: new investigation, reopen, follow, archive", async ({ page, request }) => {
  const panel = await seedPanel(request, "Is checkout latency in the workspace switcher test elevated?");
  await page.goto("/");
  const first = await (await request.get("/api/workspace")).json();
  const firstTitle: string = first.workspace.title;
  const firstId: string = first.workspace.id;
  const board = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(board).toBeVisible();

  const trigger = page.locator("[aria-haspopup=listbox]");
  await expect(trigger).toContainText(firstTitle);

  // a second browser page follows the switch without reload
  const other = await page.context().newPage();
  await other.goto("/");
  await expect(other.locator(`[data-panel-id="${panel.id}"]`)).toBeVisible();

  // New investigation "second": the board empties and the header shows the title
  await trigger.click();
  await page.getByRole("button", { name: "New investigation" }).click();
  await page.getByLabel("New investigation title").fill("second");
  await page.getByLabel("New investigation title").press("Enter");
  const header = page.locator("[aria-haspopup=listbox]");
  await expect(header).toContainText("second");
  await expect(board).toHaveCount(0);
  await expect(page.locator(".empty")).toContainText("second");
  await expect(other.locator("[aria-haspopup=listbox]")).toContainText("second");
  await expect(other.locator(`[data-panel-id="${panel.id}"]`)).toHaveCount(0);

  // reopen the first: its panel is back, on both pages
  await header.click();
  await page.locator(`[data-workspace-id="${firstId}"]`).getByRole("button", { name: /open/i }).first().click();
  await expect(board).toBeVisible();
  await expect(other.locator(`[data-panel-id="${panel.id}"]`)).toBeVisible();
  await expect(header).toContainText(firstTitle);

  // rename inline: Esc cancels, Enter saves (the row of the inactive workspace)
  await header.click();
  const row2 = page.locator(".ws-row", { hasText: "second" });
  await row2.getByRole("button", { name: /^rename/i }).click();
  await page.getByRole("textbox", { name: "Rename workspace" }).fill("discarded");
  await page.getByRole("textbox", { name: "Rename workspace" }).press("Escape");
  await expect(page.locator(".ws-row", { hasText: "second" })).toBeVisible();
  await row2.getByRole("button", { name: /^rename/i }).click();
  await page.getByRole("textbox", { name: "Rename workspace" }).fill("second renamed");
  await page.getByRole("textbox", { name: "Rename workspace" }).press("Enter");
  const renamed = page.locator(".ws-row", { hasText: "second renamed" });
  await expect(renamed).toBeVisible();
  // renaming an inactive workspace leaves the board alone
  await expect(board).toBeVisible();

  // archive it: gone until "Show archived"; the active row has no Archive
  await expect(page.locator(`[data-workspace-id="${firstId}"]`).getByRole("button", { name: /^archive/i })).toHaveCount(0);
  await renamed.getByRole("button", { name: /^archive/i }).click();
  await expect(page.locator(".ws-row", { hasText: "second renamed" })).toHaveCount(0);
  await page.getByLabel("Show archived").check();
  await expect(page.locator(".ws-row", { hasText: "second renamed" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await other.close();
});
