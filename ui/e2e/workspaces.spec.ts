import { expect, test } from "@playwright/test";
import { seedPanel } from "./helpers.js";

test("workspace switcher: new investigation, reopen, follow, archive", async ({ page, request }, testInfo) => {
  const panel = await seedPanel(request, "Is checkout latency in the workspace switcher test elevated?");
  await page.goto("/");
  const first = await (await request.get("/api/workspace")).json();
  const firstTitle: string = first.workspace.title;
  const firstId: string = first.workspace.id;
  const board = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(board).toBeVisible();

  const trigger = page.locator(".ws-trigger");
  await expect(trigger).toContainText(firstTitle);

  // a second browser page follows the switch without reload
  const other = await page.context().newPage();
  await other.goto("/");
  await expect(other.locator(`[data-panel-id="${panel.id}"]`)).toBeVisible();

  // opening the popover refetches the list (counts, "ago", rows missed while the daemon was down)
  const listed = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/workspaces");
  await trigger.click();
  await listed;

  // New investigation "second": the board empties and the header shows the title.
  // A double Enter creates one workspace, not two.
  // unique per attempt: the daemon is shared across retries, so a failed attempt leaves its rows behind
  const nonce = `${testInfo.retry}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const secondTitle = `second ${nonce}`;
  await page.getByRole("button", { name: "New investigation" }).click();
  await page.getByLabel("New investigation title").fill(secondTitle);
  await page.getByLabel("New investigation title").press("Enter");
  await page.keyboard.press("Enter");
  const header = page.locator(".ws-trigger");
  await expect(header).toContainText("second");
  await expect(board).toHaveCount(0);
  await expect(page.locator(".empty")).toContainText("second");
  await expect(other.locator(".ws-trigger")).toContainText("second");
  await expect(other.locator(`[data-panel-id="${panel.id}"]`)).toHaveCount(0);
  const all = await (await request.get("/api/workspaces")).json();
  const mine = all.workspaces.filter((w: { title: string }) => w.title === secondTitle);
  expect(mine).toHaveLength(1);
  const secondId: string = mine[0].id;

  // reopen the first: its panel is back, on both pages
  await header.click();
  await page.locator(`[data-workspace-id="${firstId}"]`).locator(".ws-open").click();
  await expect(board).toBeVisible();
  await expect(other.locator(`[data-panel-id="${panel.id}"]`)).toBeVisible();
  await expect(header).toContainText(firstTitle);

  // rename inline: Esc cancels, Enter saves (the row of the inactive workspace)
  await header.click();
  const row2 = page.locator(`[data-workspace-id="${secondId}"]`);
  await row2.getByRole("button", { name: /^rename/i }).click();
  await page.getByRole("textbox", { name: "Rename workspace" }).fill("discarded");
  await page.getByRole("textbox", { name: "Rename workspace" }).press("Escape");
  await expect(row2).toBeVisible();
  await row2.getByRole("button", { name: /^rename/i }).click();
  await page.getByRole("textbox", { name: "Rename workspace" }).fill(`${secondTitle} renamed`);
  await page.getByRole("textbox", { name: "Rename workspace" }).press("Enter");
  const renamed = page.locator(`[data-workspace-id="${secondId}"]`);
  await expect(renamed).toContainText(`${secondTitle} renamed`);
  // renaming an inactive workspace leaves the board alone
  await expect(board).toBeVisible();

  // archive it: gone until "Show archived"; the active row has no Archive
  await expect(page.locator(`[data-workspace-id="${firstId}"]`).getByRole("button", { name: /^archive/i })).toHaveCount(0);
  await renamed.getByRole("button", { name: /^archive/i }).click();
  await expect(renamed).toHaveCount(0);
  await page.getByLabel("Show archived").check();
  await expect(renamed).toContainText(`${secondTitle} renamed`);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("list", { name: "Investigations" })).toHaveCount(0);
  await other.close();
});

test("workspace switcher: focus return, plain-list a11y, archived toggle", async ({ page, request }) => {
  await seedPanel(request, "Is checkout latency in the switcher a11y test elevated?");
  await page.goto("/");
  const trigger = page.locator(".ws-trigger");
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  const popover = page.locator("#ws-popover");
  await expect(trigger).toHaveAttribute("aria-controls", "ws-popover");

  // valid pattern: a plain list, no option roles, no buttons nested in options
  await expect(popover.getByRole("list", { name: "Investigations" })).toBeVisible();
  await expect(popover.getByRole("option")).toHaveCount(0);
  // the open action's name carries the activity and finding text
  await expect(popover.locator(".ws-open").first()).toHaveAccessibleName(/ago|just now/);
  await expect(popover.locator(".ws-open").first()).toHaveAccessibleName(/\d+ findings?/);

  // Esc in the create input returns focus to New investigation; a second Esc closes and focuses the trigger
  await page.getByRole("button", { name: "New investigation" }).click();
  await expect(page.getByLabel("New investigation title")).toHaveAttribute("maxlength", "120"); // the daemon's cap
  await page.getByLabel("New investigation title").press("Escape");
  await expect(page.getByRole("button", { name: "New investigation" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(popover).toHaveCount(0);
  await expect(trigger).toBeFocused();

  // Esc in the rename input returns focus to that row's Rename button
  await trigger.click();
  const row = page.locator(".ws-row").first();
  await row.getByRole("button", { name: /^rename/i }).click();
  await expect(page.getByRole("textbox", { name: "Rename workspace" })).toHaveAttribute("maxlength", "120");
  await page.getByRole("textbox", { name: "Rename workspace" }).press("Escape");
  await expect(row.getByRole("button", { name: /^rename/i })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(popover).toHaveCount(0);
  await expect(trigger).toBeFocused();

  // outside click closes and returns focus to the trigger; "Show archived" resets on close
  await trigger.click();
  await page.getByLabel("Show archived").check();
  await page.locator("body").click({ position: { x: 1, y: 1 } });
  await expect(popover).toHaveCount(0);
  await trigger.click();
  await expect(page.getByLabel("Show archived")).not.toBeChecked();
  // toggling on keeps the current rows visible (no empty flash)
  const rows = page.locator(".ws-row");
  const before = await rows.count();
  await page.getByLabel("Show archived").check();
  await expect(rows).not.toHaveCount(0);
  expect(await rows.count()).toBeGreaterThanOrEqual(before);
});

test("workspace switcher: a slow list response from before a rename cannot revert it (8ubh)", async ({ page, request }, testInfo) => {
  await seedPanel(request, "Is checkout latency in the stale-list test elevated?");
  const title = `stale ${testInfo.retry}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const created = await request.post("/api/workspaces", { data: { title } });
  expect(created.ok()).toBeTruthy();
  const otherId: string = (await created.json()).workspace.id;
  // switch back so the created workspace is inactive
  const list = await (await request.get("/api/workspaces")).json();
  const activeId: string = list.workspaces.find((w: { id: string }) => w.id !== otherId).id;
  expect((await request.post(`/api/workspaces/${activeId}/open`, { data: {} })).ok()).toBeTruthy();
  await page.goto("/");
  await expect(page.locator(".ws-trigger")).toBeVisible();

  // hold the popover's list refetch (the pre-rename state) until the rename has landed
  let release!: () => void;
  const released = new Promise<void>((r) => (release = r));
  let held = false;
  await page.route((u) => u.pathname === "/api/workspaces" && !u.search, async (route) => {
    if (held) return route.continue();
    held = true;
    const stale = await route.fetch();
    await released;
    await route.fulfill({ response: stale });
  });
  await page.locator(".ws-trigger").click();
  await expect.poll(() => held).toBe(true);

  const row = page.locator(`[data-workspace-id="${otherId}"]`);
  await row.getByRole("button", { name: /^rename/i }).click();
  await page.getByRole("textbox", { name: "Rename workspace" }).fill(`${title} renamed`);
  await page.getByRole("textbox", { name: "Rename workspace" }).press("Enter");
  await expect(row).toContainText(`${title} renamed`);
  release();
  // the stale response lands now; the renamed row must stay
  await page.waitForTimeout(500);
  await expect(row).toContainText(`${title} renamed`);
});
