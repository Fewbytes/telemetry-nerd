import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers";

// Panel chart height resize (bead telemetry-nerd-z0mq): a drag handle on the bottom edge of a
// time/fleet/spectrogram chart, persisted per-panel in localStorage (a per-viewer preference,
// like theme — not workspace data).
test("resize: dragging the handle grows the chart and survives a reload", async ({ page }) => {
  const panel = await seedPanel(page.request, "resize: can this panel's chart grow?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  const handle = el.locator(".resize-handle");
  await expect(handle).toBeVisible();
  const before = await el.locator(".plot").boundingBox();

  const box = (await handle.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2 + 100, { steps: 5 });
  await page.mouse.up();

  const after = await el.locator(".plot").boundingBox();
  expect(after!.height).toBeGreaterThan(before!.height + 50);

  await page.reload();
  const elAfterReload = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(elAfterReload.locator("canvas").first()).toBeVisible();
  const afterReload = await elAfterReload.locator(".plot").boundingBox();
  expect(afterReload!.height).toBeGreaterThan(before!.height + 50);
});

test("resize: ArrowDown/ArrowUp on the focused handle steps the height and persists it", async ({ page }) => {
  const panel = await seedPanel(page.request, "resize: does the keyboard path work?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  const handle = el.locator(".resize-handle");
  const start = Number(await handle.getAttribute("aria-valuenow"));
  await handle.focus();
  await handle.press("ArrowDown");
  await handle.press("ArrowDown");
  await expect(handle).toHaveAttribute("aria-valuenow", String(start + 40));

  const saved = await page.evaluate((id) => window.localStorage.getItem(`tn-panel-height-${id}`), panel.id);
  expect(saved).toBe(String(start + 40));
});
